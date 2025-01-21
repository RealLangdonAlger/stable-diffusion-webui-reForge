import torch
import contextlib
import os
from ldm_patched.modules import model_management
from ldm_patched.modules import model_detection

from ldm_patched.modules.sd import VAE, CLIP, load_model_weights
import ldm_patched.modules.model_patcher
import ldm_patched.modules.utils
import ldm_patched.modules.clip_vision

from omegaconf import OmegaConf
from modules.sd_models_config import find_checkpoint_config
from modules.shared import cmd_opts
from modules import sd_hijack
from modules.sd_models_xl import extend_sdxl
from ldm.util import instantiate_from_config
from modules_forge import forge_clip
from modules_forge.unet_patcher import UnetPatcher
from ldm_patched.modules.model_base import model_sampling, ModelType, SD3
import logging
import types

import open_clip
from transformers import CLIPTextModel, CLIPTokenizer
from ldm_patched.modules.args_parser import args


class FakeObject:
    def __init__(self, *args, **kwargs):
        super().__init__()
        self.visual = None
        return

    def eval(self, *args, **kwargs):
        return self

    def parameters(self, *args, **kwargs):
        return []


class ForgeSD:
    def __init__(self, unet, clip, vae, clipvision):
        self.unet = unet
        self.clip = clip
        self.vae = vae
        self.clipvision = clipvision

    def shallow_copy(self):
        return ForgeSD(
            self.unet,
            self.clip,
            self.vae,
            self.clipvision
        )


@contextlib.contextmanager
def no_clip():
    backup_openclip = open_clip.create_model_and_transforms
    backup_CLIPTextModel = CLIPTextModel.from_pretrained
    backup_CLIPTokenizer = CLIPTokenizer.from_pretrained

    try:
        open_clip.create_model_and_transforms = lambda *args, **kwargs: (FakeObject(), None, None)
        CLIPTextModel.from_pretrained = lambda *args, **kwargs: FakeObject()
        CLIPTokenizer.from_pretrained = lambda *args, **kwargs: FakeObject()
        yield

    finally:
        open_clip.create_model_and_transforms = backup_openclip
        CLIPTextModel.from_pretrained = backup_CLIPTextModel
        CLIPTokenizer.from_pretrained = backup_CLIPTokenizer
    return


def load_checkpoint_guess_config(ckpt, output_vae=True, output_clip=True, output_clipvision=False, embedding_directory=None, output_model=True, model_options={}, te_model_options={}):
    if isinstance(ckpt, str) and os.path.isfile(ckpt):
        # If ckpt is a string and a valid file path, load it
        sd = ldm_patched.modules.utils.load_torch_file(ckpt)
        ckpt_path = ckpt  # Store the path for error reporting
    elif isinstance(ckpt, dict):
        # If ckpt is already a state dictionary, use it directly
        sd = ckpt
        ckpt_path = "provided state dict"  # Generic description for error reporting
    else:
        raise ValueError("Input must be either a file path or a state dictionary")

    out = load_state_dict_guess_config(sd, output_vae, output_clip, output_clipvision, embedding_directory, output_model, model_options, te_model_options=te_model_options)
    if out is None:
        raise RuntimeError(f"ERROR: Could not detect model type of: {ckpt_path}")
    return out

def load_state_dict_guess_config(sd, output_vae=True, output_clip=True, output_clipvision=False, embedding_directory=None, output_model=True, model_options={}, te_model_options={}):
    clip = None
    clipvision = None
    vae = None
    model = None
    model_patcher = None

    diffusion_model_prefix = model_detection.unet_prefix_from_state_dict(sd)
    parameters = ldm_patched.modules.utils.calculate_parameters(sd, diffusion_model_prefix)
    weight_dtype = ldm_patched.modules.utils.weight_dtype(sd, diffusion_model_prefix)
    load_device = model_management.get_torch_device()

    model_config = model_detection.model_config_from_unet(sd, diffusion_model_prefix)
    if model_config is None:
        return None

    unet_weight_dtype = list(model_config.supported_inference_dtypes)
    if weight_dtype is not None and model_config.scaled_fp8 is None:
        unet_weight_dtype.append(weight_dtype)

    model_config.custom_operations = model_options.get("custom_operations", model_config.custom_operations)
    if model_options.get("fp8_optimizations", False):
        model_config.optimizations["fp8"] = True
    unet_dtype = model_options.get("dtype", model_options.get("weight_dtype", None))

    if unet_dtype is None:
        unet_dtype = model_management.unet_dtype(model_params=parameters, supported_dtypes=unet_weight_dtype)

    manual_cast_dtype = model_management.unet_manual_cast(unet_dtype, load_device, model_config.supported_inference_dtypes)
    model_config.set_inference_dtype(unet_dtype, manual_cast_dtype)

    if model_config.clip_vision_prefix is not None:
        if output_clipvision:
            clipvision = ldm_patched.modules.clip_vision.load_clipvision_from_sd(sd, model_config.clip_vision_prefix, True)

    if output_model:
        inital_load_device = model_management.unet_inital_load_device(parameters, unet_dtype)
        model = model_config.get_model(sd, diffusion_model_prefix, device=inital_load_device)
        model.load_model_weights(sd, diffusion_model_prefix)

    if output_vae:
        vae_sd = ldm_patched.modules.utils.state_dict_prefix_replace(sd, {k: "" for k in model_config.vae_key_prefix}, filter_keys=True)
        vae_sd = model_config.process_vae_state_dict(vae_sd)
        vae = VAE(sd=vae_sd)

    if output_clip:
        clip_target = model_config.clip_target(state_dict=sd)
        if clip_target is not None:
            clip_sd = model_config.process_clip_state_dict(sd)
            if len(clip_sd) > 0:
                parameters = ldm_patched.modules.utils.calculate_parameters(clip_sd)
                clip = CLIP(clip_target, embedding_directory=embedding_directory, tokenizer_data=clip_sd, parameters=parameters, model_options=te_model_options)
                m, u = clip.load_sd(clip_sd, full_model=True)
                if len(m) > 0:
                    m_filter = list(filter(lambda a: ".logit_scale" not in a and ".transformer.text_projection.weight" not in a, m))
                    if len(m_filter) > 0:
                        logging.warning("clip missing: {}".format(m))
                    else:
                        logging.debug("clip missing: {}".format(m))

                if len(u) > 0:
                    logging.debug("clip unexpected {}:".format(u))
            else:
                logging.warning("no CLIP/text encoder weights in checkpoint, the text encoder model will not be loaded.")

    left_over = sd.keys()
    if len(left_over) > 0:
        logging.debug("left over keys: {}".format(left_over))

    if output_model:
        model_patcher = UnetPatcher(model, load_device=load_device, offload_device=model_management.unet_offload_device())
        if inital_load_device != torch.device("cpu"):
            logging.info("loaded straight to GPU")
            model_management.load_models_gpu([model_patcher], force_full_load=True)

    return ForgeSD(model_patcher, clip, vae, clipvision)

def compile_model(unet, backend="inductor"):
    """Compile the UNet model and store compilation settings."""
    if hasattr(torch, 'compile'):
        try:
            # Get torch version check done first
            torch_version = torch.__version__.split('.')
            if int(torch_version[0]) < 2:
                print(f"torch.compile requires PyTorch 2.0 or newer. Current version: {torch.__version__}")
                return

            # Configure dynamo
            import torch._dynamo as dynamo
            dynamo.config.suppress_errors = True
            dynamo.config.verbose = True

            compile_settings = {}
            
            if args.torch_compile_mode == "max-autotune":
                compile_settings = {
                    "backend": backend,
                    "mode": None,  # Mode is ignored when using options
                    "fullgraph": False,
                    "options": {
                        "max_autotune": True,
                        "max_autotune_gemm": True,
                        "max_autotune_pointwise": True,
                        "trace.enabled": True,
                        "trace.graph_diagram": True,
                        "epilogue_fusion": True,
                        "layout_optimization": True,
                        "aggressive_fusion": True
                    }
                }
                print("\nUsing max-autotune compilation settings:")
                for option, value in compile_settings["options"].items():
                    print(f"  - {option}: {value}")
            else:
                compile_settings = {
                    "mode": args.torch_compile_mode,
                    "backend": backend,
                    "fullgraph": False,
                    "dynamic": False
                }
                print("\nUsing standard compilation settings:")
                for setting, value in compile_settings.items():
                    print(f"  - {setting}: {value}")
            
            # Store settings before compilation
            unet.model.compile_settings = compile_settings
            
            # Compile the model
            print(f"\nStarting model compilation with backend '{backend}' and mode '{args.torch_compile_mode}'")
            unet.model = torch.compile(
                unet.model,
                **compile_settings
            )
            
            # Verify compilation
            print("\nVerifying compilation results:")
            print(f"Model type after compilation: {type(unet.model)}")
            print(f"Has _orig_mod attribute: {'_orig_mod' in dir(unet.model)}")
            if hasattr(unet.model, '_inductor_version'):
                print(f"Inductor version: {unet.model._inductor_version}")
            
            unet.compiled = True
            print("\nUNet model compilation successful")
            return True
            
        except Exception as e:
            unet.compiled = False
            print(f"\nError during model compilation: {str(e)}")
            print("Stack trace:")
            import traceback
            traceback.print_exc()
            return False
    else:
        print("Warning: torch.compile not available in this PyTorch version")
        return False


@torch.no_grad()
def load_model_for_a1111(timer, checkpoint_info=None, state_dict=None):
    is_sd3 = 'model.diffusion_model.x_embedder.proj.weight' in state_dict
    ztsnr = 'ztsnr' in state_dict
    timer.record("forge solving config")
    
    if not is_sd3:
        a1111_config_filename = find_checkpoint_config(state_dict, checkpoint_info)
        a1111_config = OmegaConf.load(a1111_config_filename)
        if hasattr(a1111_config.model.params, 'network_config'):
            a1111_config.model.params.network_config.target = 'modules_forge.forge_loader.FakeObject'
        if hasattr(a1111_config.model.params, 'unet_config'):
            a1111_config.model.params.unet_config.target = 'modules_forge.forge_loader.FakeObject'
        if hasattr(a1111_config.model.params, 'first_stage_config'):
            a1111_config.model.params.first_stage_config.target = 'modules_forge.forge_loader.FakeObject'
        with no_clip():
            sd_model = instantiate_from_config(a1111_config.model)
    else:
        sd_model = torch.nn.Module() 
    
    timer.record("forge instantiate config")
    
    forge_objects = load_checkpoint_guess_config(
        state_dict,
        output_vae=True,
        output_clip=True,
        output_clipvision=True,
        embedding_directory=cmd_opts.embeddings_dir,
        output_model=True
    )
    
    sd_model.forge_objects = forge_objects
    sd_model.forge_objects_original = forge_objects.shallow_copy()
    sd_model.forge_objects_after_applying_lora = forge_objects.shallow_copy()
    sd_model.first_stage_model = forge_objects.vae.first_stage_model
    sd_model.model.diffusion_model = forge_objects.unet.model.diffusion_model

    if args.torch_compile:
        timer.record("start model compilation")
        if forge_objects.unet is not None:
            compile_model(forge_objects.unet, backend=args.torch_compile_backend)
        timer.record("model compilation complete")
    timer.record("forge load real models")
    
    conditioner = getattr(sd_model, 'conditioner', None)

    if conditioner:
        text_cond_models = []
        for i in range(len(conditioner.embedders)):
            embedder = conditioner.embedders[i]
            typename = type(embedder).__name__
            if typename == 'FrozenCLIPEmbedder':  # SDXL Clip L
                embedder.tokenizer = forge_objects.clip.tokenizer.clip_l.tokenizer
                embedder.transformer = forge_objects.clip.cond_stage_model.clip_l.transformer
                model_embeddings = embedder.transformer.text_model.embeddings
                model_embeddings.token_embedding = sd_hijack.EmbeddingsWithFixes(
                    model_embeddings.token_embedding, sd_hijack.model_hijack)
                embedder = forge_clip.CLIP_SD_XL_L(embedder, sd_hijack.model_hijack)
                conditioner.embedders[i] = embedder
                text_cond_models.append(embedder)
            elif typename == 'FrozenOpenCLIPEmbedder2':  # SDXL Clip G
                embedder.tokenizer = forge_objects.clip.tokenizer.clip_g.tokenizer
                embedder.transformer = forge_objects.clip.cond_stage_model.clip_g.transformer
                embedder.text_projection = forge_objects.clip.cond_stage_model.clip_g.text_projection
                model_embeddings = embedder.transformer.text_model.embeddings
                model_embeddings.token_embedding = sd_hijack.EmbeddingsWithFixes(
                    model_embeddings.token_embedding, sd_hijack.model_hijack, textual_inversion_key='clip_g')
                embedder = forge_clip.CLIP_SD_XL_G(embedder, sd_hijack.model_hijack)
                conditioner.embedders[i] = embedder
                text_cond_models.append(embedder)
        if len(text_cond_models) == 1:
            sd_model.cond_stage_model = text_cond_models[0]
        else:
            sd_model.cond_stage_model = conditioner
    elif type(sd_model.cond_stage_model).__name__ == 'FrozenCLIPEmbedder':  # SD15 Clip
        sd_model.cond_stage_model.tokenizer = forge_objects.clip.tokenizer.clip_l.tokenizer
        sd_model.cond_stage_model.transformer = forge_objects.clip.cond_stage_model.clip_l.transformer
        model_embeddings = sd_model.cond_stage_model.transformer.text_model.embeddings
        model_embeddings.token_embedding = sd_hijack.EmbeddingsWithFixes(
            model_embeddings.token_embedding, sd_hijack.model_hijack)
        sd_model.cond_stage_model = forge_clip.CLIP_SD_15_L(sd_model.cond_stage_model, sd_hijack.model_hijack)
    elif type(sd_model.cond_stage_model).__name__ == 'FrozenOpenCLIPEmbedder':  # SD21 Clip
        sd_model.cond_stage_model.tokenizer = forge_objects.clip.tokenizer.clip_h.tokenizer
        sd_model.cond_stage_model.transformer = forge_objects.clip.cond_stage_model.clip_h.transformer
        model_embeddings = sd_model.cond_stage_model.transformer.text_model.embeddings
        model_embeddings.token_embedding = sd_hijack.EmbeddingsWithFixes(
            model_embeddings.token_embedding, sd_hijack.model_hijack)
        sd_model.cond_stage_model = forge_clip.CLIP_SD_21_H(sd_model.cond_stage_model, sd_hijack.model_hijack)
    else:
        raise NotImplementedError('Bad Clip Class Name:' + type(sd_model.cond_stage_model).__name__)

    timer.record("forge set components")
    sd_model_hash = checkpoint_info.calculate_shorthash()
    timer.record("calculate hash")

    if getattr(sd_model, 'parameterization', None) == 'v':
        sd_model.forge_objects.unet.model.model_sampling = model_sampling(sd_model.forge_objects.unet.model.model_config, ModelType.V_PREDICTION)
    
    sd_model.ztsnr = ztsnr

    sd_model.is_sd3 = is_sd3
    sd_model.latent_channels = 16 if is_sd3 else 4
    sd_model.is_sdxl = conditioner is not None and not is_sd3
    sd_model.is_sdxl_inpaint = sd_model.is_sdxl and forge_objects.unet.model.diffusion_model.in_channels == 9
    sd_model.is_sd2 = not sd_model.is_sdxl and not is_sd3 and hasattr(sd_model.cond_stage_model, 'model')
    sd_model.is_sd1 = not sd_model.is_sdxl and not sd_model.is_sd2 and not is_sd3
    sd_model.is_ssd = sd_model.is_sdxl and 'model.diffusion_model.middle_block.1.transformer_blocks.0.attn1.to_q.weight' not in sd_model.state_dict().keys()
    
    if sd_model.is_sdxl:
        extend_sdxl(sd_model)
    
    sd_model.sd_model_hash = sd_model_hash
    sd_model.sd_model_checkpoint = checkpoint_info.filename
    sd_model.sd_checkpoint_info = checkpoint_info

    @torch.inference_mode()
    def patched_decode_first_stage(x):
        sample = sd_model.forge_objects.unet.model.model_config.latent_format.process_out(x)
        sample = sd_model.forge_objects.vae.decode(sample).movedim(-1, 1) * 2.0 - 1.0
        return sample.to(x)

    @torch.inference_mode()
    def patched_encode_first_stage(x):
        sample = sd_model.forge_objects.vae.encode(x.movedim(1, -1) * 0.5 + 0.5)
        sample = sd_model.forge_objects.unet.model.model_config.latent_format.process_in(sample)
        return sample.to(x)

    sd_model.ema_scope = lambda *args, **kwargs: contextlib.nullcontext()
    sd_model.get_first_stage_encoding = lambda x: x
    sd_model.decode_first_stage = patched_decode_first_stage
    sd_model.encode_first_stage = patched_encode_first_stage
    sd_model.clip = sd_model.cond_stage_model
    sd_model.tiling_enabled = False
    timer.record("forge finalize")
    sd_model.current_lora_hash = str([])
    return sd_model
