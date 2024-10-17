import torch
from enum import Enum
import logging

from ldm_patched.modules import model_management
from ldm_patched.ldm.models.autoencoder import AutoencoderKL, AutoencodingEngine
from ldm_patched.ldm.cascade.stage_a import StageA
from ldm_patched.ldm.cascade.stage_c_coder import StageC_coder
from ldm_patched.ldm.audio.autoencoder import AudioOobleckVAE
import yaml

import ldm_patched.modules.utils

from . import clip_vision
from . import gligen
from . import diffusers_convert
from . import model_detection

from . import sd1_clip
from . import sdxl_clip
import ldm_patched.modules.text_encoders.sd2_clip
import ldm_patched.modules.text_encoders.sd3_clip
import ldm_patched.modules.text_encoders.sa_t5
import ldm_patched.modules.text_encoders.aura_t5
import ldm_patched.modules.text_encoders.hydit
import ldm_patched.modules.text_encoders.flux

import ldm_patched.modules.model_patcher
import ldm_patched.modules.lora
import ldm_patched.t2ia.adapter
import ldm_patched.modules.supported_models_base
import ldm_patched.taesd.taesd
from modules import shared

def load_model_weights(model, sd):
    m, u = model.load_state_dict(sd, strict=False)
    m = set(m)
    unexpected_keys = set(u)

    k = list(sd.keys())
    for x in k:
        if x not in unexpected_keys:
            w = sd.pop(x)
            del w
    if len(m) > 0:
        logging.warning("missing {}".format(m))
    return model

def load_clip_weights(model, sd):
    k = list(sd.keys())
    for x in k:
        if x.startswith("cond_stage_model.transformer.") and not x.startswith("cond_stage_model.transformer.text_model."):
            y = x.replace("cond_stage_model.transformer.", "cond_stage_model.transformer.text_model.")
            sd[y] = sd.pop(x)

    if 'cond_stage_model.transformer.text_model.embeddings.position_ids' in sd:
        ids = sd['cond_stage_model.transformer.text_model.embeddings.position_ids']
        if ids.dtype == torch.float32:
            sd['cond_stage_model.transformer.text_model.embeddings.position_ids'] = ids.round()

    sd = ldm_patched.modules.utils.transformers_convert(sd, "cond_stage_model.model.", "cond_stage_model.transformer.text_model.", 24)
    return load_model_weights(model, sd)

def load_lora_for_models(model, clip, lora, strength_model, strength_clip, filename='default'):
    key_map = {}
    if model is not None:
        key_map = ldm_patched.modules.lora.model_lora_keys_unet(model.model, key_map)
    if clip is not None:
        key_map = ldm_patched.modules.lora.model_lora_keys_clip(clip.cond_stage_model, key_map)

    loaded = ldm_patched.modules.lora.load_lora(lora, key_map)
    
    model_flag = type(model.model).__name__ if model is not None else 'default'
    
    if model is not None:
        new_modelpatcher = model.clone()
        loaded_keys_unet = new_modelpatcher.add_patches(loaded, strength_model)
    else:
        new_modelpatcher = None
        loaded_keys_unet = set()

    if clip is not None:
        new_clip = clip.clone()
        loaded_keys_clip = new_clip.add_patches(loaded, strength_clip)
    else:
        new_clip = None
        loaded_keys_clip = set()

    total_loaded_keys = len(loaded_keys_unet) + len(loaded_keys_clip)
    print(f'[LORA] Loaded {filename} for {model_flag} with {total_loaded_keys} keys (UNet: {len(loaded_keys_unet)}, CLIP: {len(loaded_keys_clip)}) at weight {strength_clip}')

    return (new_modelpatcher, new_clip)


class CLIP:
    def __init__(self, target=None, embedding_directory=None, no_init=False, tokenizer_data={}, parameters=0, model_options={}):
        if no_init:
            return
        params = target.params.copy()
        clip = target.clip
        tokenizer = target.tokenizer

        load_device = model_options.get("load_device", model_management.text_encoder_device())
        offload_device = model_options.get("offload_device", model_management.text_encoder_offload_device())
        dtype = model_options.get("dtype", None)
        if dtype is None:
            dtype = model_management.text_encoder_dtype(load_device)
        params['dtype'] = dtype
        params['device'] = model_options.get("initial_device", model_management.text_encoder_initial_device(load_device, offload_device, parameters * model_management.dtype_size(dtype)))
        params['model_options'] = model_options

        self.cond_stage_model = clip(**(params))

        if shared.opts.cond_stage_model_device_compatibility_check:
            for dt in self.cond_stage_model.dtypes:
                if not model_management.supports_cast(load_device, dt):
                    load_device = offload_device
                    if params['device'] != offload_device:
                        self.cond_stage_model.to(offload_device)
                        logging.warning("Had to shift TE back.")
                    print(f"Conditional stage model dtype {dt} not supported. Falling back to {offload_device}.")
                    break
        else:
            logging.info("Conditional stage model device compatibility check is disabled.")

        self.tokenizer = tokenizer(embedding_directory=embedding_directory, tokenizer_data=tokenizer_data)
        self.patcher = ldm_patched.modules.model_patcher.ModelPatcher(self.cond_stage_model, load_device=load_device, offload_device=offload_device)
        if params['device'] == load_device:
            model_management.load_models_gpu([self.patcher], force_full_load=True)
        self.layer_idx = None
        logging.debug("CLIP model load device: {}, offload device: {}, current: {}".format(load_device, offload_device, params['device']))

    def clone(self):
        n = CLIP(no_init=True)
        n.patcher = self.patcher.clone()
        n.cond_stage_model = self.cond_stage_model
        n.tokenizer = self.tokenizer
        n.layer_idx = self.layer_idx
        return n

    def add_patches(self, patches, strength_patch=1.0, strength_model=1.0):
        return self.patcher.add_patches(patches, strength_patch, strength_model)

    def clip_layer(self, layer_idx):
        self.layer_idx = layer_idx

    def tokenize(self, text, return_word_ids=False):
        return self.tokenizer.tokenize_with_weights(text, return_word_ids)

    def encode_from_tokens(self, tokens, return_pooled=False, return_dict=False):
        self.cond_stage_model.reset_clip_options()

        if self.layer_idx is not None:
            self.cond_stage_model.set_clip_options({"layer": self.layer_idx})

        if return_pooled == "unprojected":
            self.cond_stage_model.set_clip_options({"projected_pooled": False})

        self.load_model()
        o = self.cond_stage_model.encode_token_weights(tokens)
        cond, pooled = o[:2]
        if return_dict:
            out = {"cond": cond, "pooled_output": pooled}
            if len(o) > 2:
                for k in o[2]:
                    out[k] = o[2][k]
            return out

        if return_pooled:
            return cond, pooled
        return cond

    def encode(self, text):
        tokens = self.tokenize(text)
        return self.encode_from_tokens(tokens)

    def load_sd(self, sd, full_model=False):
        if full_model:
            return self.cond_stage_model.load_state_dict(sd, strict=False)
        else:
            return self.cond_stage_model.load_sd(sd)

    def get_sd(self):
        sd_clip = self.cond_stage_model.state_dict()
        sd_tokenizer = self.tokenizer.state_dict()
        for k in sd_tokenizer:
            sd_clip[k] = sd_tokenizer[k]
        return sd_clip

    def load_model(self):
        model_management.load_model_gpu(self.patcher)
        return self.patcher

    def get_key_patches(self):
        return self.patcher.get_key_patches()

class VAE:
    def __init__(self, sd=None, device=None, config=None, dtype=None):
        if 'decoder.up_blocks.0.resnets.0.norm1.weight' in sd.keys(): #diffusers format
            sd = diffusers_convert.convert_vae_state_dict(sd)

        self.memory_used_encode = lambda shape, dtype: (1767 * shape[2] * shape[3]) * model_management.dtype_size(dtype) #These are for AutoencoderKL and need tweaking (should be lower)
        self.memory_used_decode = lambda shape, dtype: (2178 * shape[2] * shape[3] * 64) * model_management.dtype_size(dtype)
        self.downscale_ratio = 8
        self.upscale_ratio = 8
        self.latent_channels = 4
        self.output_channels = 3
        self.process_input = lambda image: image * 2.0 - 1.0
        self.process_output = lambda image: torch.clamp((image + 1.0) / 2.0, min=0.0, max=1.0)
        self.working_dtypes = [torch.bfloat16, torch.float32]

        if config is None:
            if "decoder.mid.block_1.mix_factor" in sd:
                encoder_config = {'double_z': True, 'z_channels': 4, 'resolution': 256, 'in_channels': 3, 'out_ch': 3, 'ch': 128, 'ch_mult': [1, 2, 4, 4], 'num_res_blocks': 2, 'attn_resolutions': [], 'dropout': 0.0}
                decoder_config = encoder_config.copy()
                decoder_config["video_kernel_size"] = [3, 1, 1]
                decoder_config["alpha"] = 0.0
                self.first_stage_model = AutoencodingEngine(regularizer_config={'target': "ldm_patched.ldm.models.autoencoder.DiagonalGaussianRegularizer"},
                                                            encoder_config={'target': "ldm_patched.ldm.modules.diffusionmodules.model.Encoder", 'params': encoder_config},
                                                            decoder_config={'target': "ldm_patched.ldm.modules.temporal_ae.VideoDecoder", 'params': decoder_config})
            elif "taesd_decoder.1.weight" in sd:
                self.latent_channels = sd["taesd_decoder.1.weight"].shape[1]
                self.first_stage_model = ldm_patched.taesd.taesd.TAESD(latent_channels=self.latent_channels)
            elif "vquantizer.codebook.weight" in sd: #VQGan: stage a of stable cascade
                self.first_stage_model = StageA()
                self.downscale_ratio = 4
                self.upscale_ratio = 4
                #TODO
                #self.memory_used_encode
                #self.memory_used_decode
                self.process_input = lambda image: image
                self.process_output = lambda image: image
            elif "backbone.1.0.block.0.1.num_batches_tracked" in sd: #effnet: encoder for stage c latent of stable cascade
                self.first_stage_model = StageC_coder()
                self.downscale_ratio = 32
                self.latent_channels = 16
                new_sd = {}
                for k in sd:
                    new_sd["encoder.{}".format(k)] = sd[k]
                sd = new_sd
            elif "blocks.11.num_batches_tracked" in sd: #previewer: decoder for stage c latent of stable cascade
                self.first_stage_model = StageC_coder()
                self.latent_channels = 16
                new_sd = {}
                for k in sd:
                    new_sd["previewer.{}".format(k)] = sd[k]
                sd = new_sd
            elif "encoder.backbone.1.0.block.0.1.num_batches_tracked" in sd: #combined effnet and previewer for stable cascade
                self.first_stage_model = StageC_coder()
                self.downscale_ratio = 32
                self.latent_channels = 16
            elif "decoder.conv_in.weight" in sd:
                #default SD1.x/SD2.x VAE parameters
                ddconfig = {'double_z': True, 'z_channels': 4, 'resolution': 256, 'in_channels': 3, 'out_ch': 3, 'ch': 128, 'ch_mult': [1, 2, 4, 4], 'num_res_blocks': 2, 'attn_resolutions': [], 'dropout': 0.0}

                if 'encoder.down.2.downsample.conv.weight' not in sd and 'decoder.up.3.upsample.conv.weight' not in sd: #Stable diffusion x4 upscaler VAE
                    ddconfig['ch_mult'] = [1, 2, 4]
                    self.downscale_ratio = 4
                    self.upscale_ratio = 4

                self.latent_channels = ddconfig['z_channels'] = sd["decoder.conv_in.weight"].shape[1]
                if 'quant_conv.weight' in sd:
                    self.first_stage_model = AutoencoderKL(ddconfig=ddconfig, embed_dim=4)
                else:
                    self.first_stage_model = AutoencodingEngine(regularizer_config={'target': "ldm_patched.ldm.models.autoencoder.DiagonalGaussianRegularizer"},
                                                                encoder_config={'target': "ldm_patched.ldm.modules.diffusionmodules.model.Encoder", 'params': ddconfig},
                                                                decoder_config={'target': "ldm_patched.ldm.modules.diffusionmodules.model.Decoder", 'params': ddconfig})
            elif "decoder.layers.1.layers.0.beta" in sd:
                self.first_stage_model = AudioOobleckVAE()
                self.memory_used_encode = lambda shape, dtype: (1000 * shape[2]) * model_management.dtype_size(dtype)
                self.memory_used_decode = lambda shape, dtype: (1000 * shape[2] * 2048) * model_management.dtype_size(dtype)
                self.latent_channels = 64
                self.output_channels = 2
                self.upscale_ratio = 2048
                self.downscale_ratio =  2048
                self.process_output = lambda audio: audio
                self.process_input = lambda audio: audio
                self.working_dtypes = [torch.float16, torch.bfloat16, torch.float32]
            else:
                logging.warning("WARNING: No VAE weights detected, VAE not initalized.")
                self.first_stage_model = None
                return
        else:
            self.first_stage_model = AutoencoderKL(**(config['params']))
        self.first_stage_model = self.first_stage_model.eval()

        m, u = self.first_stage_model.load_state_dict(sd, strict=False)
        if len(m) > 0:
            logging.warning("Missing VAE keys {}".format(m))

        if len(u) > 0:
            logging.debug("Leftover VAE keys {}".format(u))

        if device is None:
            device = model_management.vae_device()
        self.device = device
        offload_device = model_management.vae_offload_device()
        if dtype is None:
            dtype = model_management.vae_dtype(self.device, self.working_dtypes)
        self.vae_dtype = dtype
        self.first_stage_model.to(self.vae_dtype)
        self.output_device = model_management.intermediate_device()

        self.patcher = ldm_patched.modules.model_patcher.ModelPatcher(self.first_stage_model, load_device=self.device, offload_device=offload_device)
        logging.debug("VAE load device: {}, offload device: {}, dtype: {}".format(self.device, offload_device, self.vae_dtype))

    def clone(self):
        n = VAE(no_init=True)
        n.patcher = self.patcher.clone()
        n.memory_used_encode = self.memory_used_encode
        n.memory_used_decode = self.memory_used_decode
        n.downscale_ratio = self.downscale_ratio
        n.upscale_ratio = self.upscale_ratio
        n.process_output = self.process_output
        n.output_channels = self.output_channels
        n.latent_channels = self.latent_channels
        n.first_stage_model = self.first_stage_model
        n.process_input = self.process_input
        n.working_dtypes = self.working_dtypes
        n.device = self.device
        n.vae_dtype = self.vae_dtype
        n.output_device = self.output_device
        return n

    def vae_encode_crop_pixels(self, pixels):
        dims = pixels.shape[1:-1]
        for d in range(len(dims)):
            x = (dims[d] // self.downscale_ratio) * self.downscale_ratio
            x_offset = (dims[d] % self.downscale_ratio) // 2
            if x != dims[d]:
                pixels = pixels.narrow(d + 1, x_offset, x)
        return pixels

    def decode_tiled_(self, samples, tile_x=64, tile_y=64, overlap = 16):
        steps = samples.shape[0] * ldm_patched.modules.utils.get_tiled_scale_steps(samples.shape[3], samples.shape[2], tile_x, tile_y, overlap)
        steps += samples.shape[0] * ldm_patched.modules.utils.get_tiled_scale_steps(samples.shape[3], samples.shape[2], tile_x // 2, tile_y * 2, overlap)
        steps += samples.shape[0] * ldm_patched.modules.utils.get_tiled_scale_steps(samples.shape[3], samples.shape[2], tile_x * 2, tile_y // 2, overlap)
        pbar = ldm_patched.modules.utils.ProgressBar(steps)

        decode_fn = lambda a: self.first_stage_model.decode(a.to(self.vae_dtype).to(self.device)).float()
        output = self.process_output(
            (ldm_patched.modules.utils.tiled_scale(samples, decode_fn, tile_x // 2, tile_y * 2, overlap, upscale_amount = self.upscale_ratio, output_device=self.output_device, pbar = pbar) +
            ldm_patched.modules.utils.tiled_scale(samples, decode_fn, tile_x * 2, tile_y // 2, overlap, upscale_amount = self.upscale_ratio, output_device=self.output_device, pbar = pbar) +
             ldm_patched.modules.utils.tiled_scale(samples, decode_fn, tile_x, tile_y, overlap, upscale_amount = self.upscale_ratio, output_device=self.output_device, pbar = pbar))
            / 3.0)
        return output

    def decode_tiled_1d(self, samples, tile_x=128, overlap=32):
        decode_fn = lambda a: self.first_stage_model.decode(a.to(self.vae_dtype).to(self.device)).float()
        return ldm_patched.modules.utils.tiled_scale_multidim(samples, decode_fn, tile=(tile_x,), overlap=overlap, upscale_amount=self.upscale_ratio, out_channels=self.output_channels, output_device=self.output_device)

    def encode_tiled_(self, pixel_samples, tile_x=512, tile_y=512, overlap = 64):
        steps = pixel_samples.shape[0] * ldm_patched.modules.utils.get_tiled_scale_steps(pixel_samples.shape[3], pixel_samples.shape[2], tile_x, tile_y, overlap)
        steps += pixel_samples.shape[0] * ldm_patched.modules.utils.get_tiled_scale_steps(pixel_samples.shape[3], pixel_samples.shape[2], tile_x // 2, tile_y * 2, overlap)
        steps += pixel_samples.shape[0] * ldm_patched.modules.utils.get_tiled_scale_steps(pixel_samples.shape[3], pixel_samples.shape[2], tile_x * 2, tile_y // 2, overlap)
        pbar = ldm_patched.modules.utils.ProgressBar(steps)

        encode_fn = lambda a: self.first_stage_model.encode((self.process_input(a)).to(self.vae_dtype).to(self.device)).float()
        samples = ldm_patched.modules.utils.tiled_scale(pixel_samples, encode_fn, tile_x, tile_y, overlap, upscale_amount = (1/self.downscale_ratio), out_channels=self.latent_channels, output_device=self.output_device, pbar=pbar)
        samples += ldm_patched.modules.utils.tiled_scale(pixel_samples, encode_fn, tile_x * 2, tile_y // 2, overlap, upscale_amount = (1/self.downscale_ratio), out_channels=self.latent_channels, output_device=self.output_device, pbar=pbar)
        samples += ldm_patched.modules.utils.tiled_scale(pixel_samples, encode_fn, tile_x // 2, tile_y * 2, overlap, upscale_amount = (1/self.downscale_ratio), out_channels=self.latent_channels, output_device=self.output_device, pbar=pbar)
        samples /= 3.0
        return samples

    def encode_tiled_1d(self, samples, tile_x=128 * 2048, overlap=32 * 2048):
        encode_fn = lambda a: self.first_stage_model.encode((self.process_input(a)).to(self.vae_dtype).to(self.device)).float()
        return ldm_patched.modules.utils.tiled_scale_multidim(samples, encode_fn, tile=(tile_x,), overlap=overlap, upscale_amount=(1/self.downscale_ratio), out_channels=self.latent_channels, output_device=self.output_device)

    def decode(self, samples_in):
        try:
            memory_used = self.memory_used_decode(samples_in.shape, self.vae_dtype)
            model_management.load_models_gpu([self.patcher], memory_required=memory_used)
            free_memory = model_management.get_free_memory(self.device)
            batch_number = int(free_memory / memory_used)
            batch_number = max(1, batch_number)

            pixel_samples = torch.empty((samples_in.shape[0], self.output_channels) + tuple(map(lambda a: a * self.upscale_ratio, samples_in.shape[2:])), device=self.output_device)
            for x in range(0, samples_in.shape[0], batch_number):
                samples = samples_in[x:x+batch_number].to(self.vae_dtype).to(self.device)
                pixel_samples[x:x+batch_number] = self.process_output(self.first_stage_model.decode(samples).to(self.output_device).float())
        except model_management.OOM_EXCEPTION as e:
            logging.warning("Warning: Ran out of memory when regular VAE decoding, retrying with tiled VAE decoding.")
            if len(samples_in.shape) == 3:
                pixel_samples = self.decode_tiled_1d(samples_in)
            else:
                pixel_samples = self.decode_tiled_(samples_in)

        pixel_samples = pixel_samples.to(self.output_device).movedim(1,-1)
        return pixel_samples

    def decode_tiled(self, samples, tile_x=64, tile_y=64, overlap = 16):
        model_management.load_model_gpu(self.patcher)
        output = self.decode_tiled_(samples, tile_x, tile_y, overlap)
        return output.movedim(1,-1)

    def encode(self, pixel_samples):
        pixel_samples = self.vae_encode_crop_pixels(pixel_samples)
        pixel_samples = pixel_samples.movedim(-1,1)
        try:
            memory_used = self.memory_used_encode(pixel_samples.shape, self.vae_dtype)
            model_management.load_models_gpu([self.patcher], memory_required=memory_used)
            free_memory = model_management.get_free_memory(self.device)
            batch_number = int(free_memory / memory_used)
            batch_number = max(1, batch_number)
            samples = torch.empty((pixel_samples.shape[0], self.latent_channels) + tuple(map(lambda a: a // self.downscale_ratio, pixel_samples.shape[2:])), device=self.output_device)
            for x in range(0, pixel_samples.shape[0], batch_number):
                pixels_in = self.process_input(pixel_samples[x:x+batch_number]).to(self.vae_dtype).to(self.device)
                samples[x:x+batch_number] = self.first_stage_model.encode(pixels_in).to(self.output_device).float()

        except model_management.OOM_EXCEPTION as e:
            logging.warning("Warning: Ran out of memory when regular VAE encoding, retrying with tiled VAE encoding.")
            if len(pixel_samples.shape) == 3:
                samples = self.encode_tiled_1d(pixel_samples)
            else:
                samples = self.encode_tiled_(pixel_samples)

        return samples

    def encode_tiled(self, pixel_samples, tile_x=512, tile_y=512, overlap = 64):
        pixel_samples = self.vae_encode_crop_pixels(pixel_samples)
        model_management.load_model_gpu(self.patcher)
        pixel_samples = pixel_samples.movedim(-1,1)
        samples = self.encode_tiled_(pixel_samples, tile_x=tile_x, tile_y=tile_y, overlap=overlap)
        return samples

    def get_sd(self):
        return self.first_stage_model.state_dict()

class StyleModel:
    def __init__(self, model, device="cpu"):
        self.model = model

    def get_cond(self, input):
        return self.model(input.last_hidden_state)


def load_style_model(ckpt_path):
    model_data = ldm_patched.modules.utils.load_torch_file(ckpt_path, safe_load=True)
    keys = model_data.keys()
    if "style_embedding" in keys:
        model = ldm_patched.t2ia.adapter.StyleAdapter(width=1024, context_dim=768, num_head=8, n_layes=3, num_token=8)
    else:
        raise Exception("invalid style model {}".format(ckpt_path))
    model.load_state_dict(model_data)
    return StyleModel(model)

class CLIPType(Enum):
    STABLE_DIFFUSION = 1
    STABLE_CASCADE = 2
    SD3 = 3
    STABLE_AUDIO = 4
    HUNYUAN_DIT = 5
    FLUX = 6

def load_clip(ckpt_paths, embedding_directory=None, clip_type=CLIPType.STABLE_DIFFUSION, model_options={}):
    clip_data = []
    for p in ckpt_paths:
        clip_data.append(ldm_patched.modules.utils.load_torch_file(p, safe_load=True))
    return load_text_encoder_state_dicts(clip_data, embedding_directory=embedding_directory, clip_type=clip_type, model_options=model_options)

class TEModel(Enum):
    CLIP_L = 1
    CLIP_H = 2
    CLIP_G = 3
    T5_XXL = 4
    T5_XL = 5
    T5_BASE = 6
def detect_te_model(sd):
    if "text_model.encoder.layers.30.mlp.fc1.weight" in sd:
        return TEModel.CLIP_G
    if "text_model.encoder.layers.22.mlp.fc1.weight" in sd:
        return TEModel.CLIP_H
    if "text_model.encoder.layers.0.mlp.fc1.weight" in sd:
        return TEModel.CLIP_L
    if "encoder.block.23.layer.1.DenseReluDense.wi_1.weight" in sd:
        weight = sd["encoder.block.23.layer.1.DenseReluDense.wi_1.weight"]
        if weight.shape[-1] == 4096:
            return TEModel.T5_XXL
        elif weight.shape[-1] == 2048:
            return TEModel.T5_XL
    if "encoder.block.0.layer.0.SelfAttention.k.weight" in sd:
        return TEModel.T5_BASE
    return None

def load_text_encoder_state_dicts(state_dicts=[], embedding_directory=None, clip_type=CLIPType.STABLE_DIFFUSION, model_options={}):
    clip_data = state_dicts
    class EmptyClass:
        pass

    for i in range(len(clip_data)):
        if "transformer.resblocks.0.ln_1.weight" in clip_data[i]:
            clip_data[i] = ldm_patched.modules.utils.clip_text_transformers_convert(clip_data[i], "", "")
        else:
            if "text_projection" in clip_data[i]:
                clip_data[i]["text_projection.weight"] = clip_data[i]["text_projection"].transpose(0, 1) #old models saved with the CLIPSave node

    clip_target = EmptyClass()
    clip_target.params = {}
    if len(clip_data) == 1:
        te_model = detect_te_model(clip_data[0])
        if te_model == TEModel.CLIP_G:
            if clip_type == CLIPType.STABLE_CASCADE:
                clip_target.clip = sdxl_clip.StableCascadeClipModel
                clip_target.tokenizer = sdxl_clip.StableCascadeTokenizer
            else:
                clip_target.clip = sdxl_clip.SDXLRefinerClipModel
                clip_target.tokenizer = sdxl_clip.SDXLTokenizer
        elif te_model == TEModel.CLIP_H:
            clip_target.clip = ldm_patched.modules.text_encoders.sd2_clip.SD2ClipModel
            clip_target.tokenizer = ldm_patched.modules.text_encoders.sd2_clip.SD2Tokenizer
        elif te_model == TEModel.T5_XXL:
            weight = clip_data[0]["encoder.block.23.layer.1.DenseReluDense.wi_1.weight"]
            dtype_t5 = weight.dtype
            clip_target.clip = ldm_patched.modules.text_encoders.sd3_clip.sd3_clip(clip_l=False, clip_g=False, t5=True, dtype_t5=dtype_t5)
            clip_target.tokenizer = ldm_patched.modules.text_encoders.sd3_clip.SD3Tokenizer
        elif te_model == TEModel.T5_XL:
            clip_target.clip = ldm_patched.modules.text_encoders.aura_t5.AuraT5Model
            clip_target.tokenizer = ldm_patched.modules.text_encoders.aura_t5.AuraT5Tokenizer
        elif te_model == TEModel.T5_BASE:
            clip_target.clip = ldm_patched.modules.text_encoders.sa_t5.SAT5Model
            clip_target.tokenizer = ldm_patched.modules.text_encoders.sa_t5.SAT5Tokenizer
        else:
            clip_target.clip = sd1_clip.SD1ClipModel
            clip_target.tokenizer = sd1_clip.SD1Tokenizer
    elif len(clip_data) == 2:
        if clip_type == CLIPType.SD3:
            clip_target.clip = ldm_patched.modules.text_encoders.sd3_clip.sd3_clip(clip_l=True, clip_g=True, t5=False)
            clip_target.tokenizer = ldm_patched.modules.text_encoders.sd3_clip.SD3Tokenizer
        elif clip_type == CLIPType.HUNYUAN_DIT:
            clip_target.clip = ldm_patched.modules.text_encoders.hydit.HyditModel
            clip_target.tokenizer = ldm_patched.modules.text_encoders.hydit.HyditTokenizer
        elif clip_type == CLIPType.FLUX:
            weight_name = "encoder.block.23.layer.1.DenseReluDense.wi_1.weight"
            weight = clip_data[0].get(weight_name, clip_data[1].get(weight_name, None))
            dtype_t5 = None
            if weight is not None:
                dtype_t5 = weight.dtype

            clip_target.clip = ldm_patched.modules.text_encoders.flux.flux_clip(dtype_t5=dtype_t5)
            clip_target.tokenizer = ldm_patched.modules.text_encoders.flux.FluxTokenizer
        else:
            clip_target.clip = sdxl_clip.SDXLClipModel
            clip_target.tokenizer = sdxl_clip.SDXLTokenizer
    elif len(clip_data) == 3:
        clip_target.clip = ldm_patched.modules.text_encoders.sd3_clip.SD3ClipModel
        clip_target.tokenizer = ldm_patched.modules.text_encoders.sd3_clip.SD3Tokenizer

    parameters = 0
    # tokenizer_data = {}
    for c in clip_data:
        parameters += ldm_patched.modules.utils.calculate_parameters(c)
        # tokenizer_data, model_options = ldm_patched.modules.text_encoders.long_clipl.model_options_long_clip(c, tokenizer_data, model_options)

    clip = CLIP(clip_target, embedding_directory=embedding_directory, parameters=parameters, model_options=model_options)
    for c in clip_data:
        m, u = clip.load_sd(c)
        if len(m) > 0:
            logging.warning("clip missing: {}".format(m))

        if len(u) > 0:
            logging.debug("clip unexpected: {}".format(u))
    return clip

def load_gligen(ckpt_path):
    data = ldm_patched.modules.utils.load_torch_file(ckpt_path, safe_load=True)
    model = gligen.load_gligen(data)
    if model_management.should_use_fp16():
        model = model.half()
    return ldm_patched.modules.model_patcher.ModelPatcher(model, load_device=model_management.get_torch_device(), offload_device=model_management.unet_offload_device())

def load_checkpoint(config_path=None, ckpt_path=None, output_vae=True, output_clip=True, embedding_directory=None, state_dict=None, config=None):
    logging.warning("Warning: The load checkpoint with config function is deprecated and will eventually be removed, please use the other one.")
    model, clip, vae, _ = load_checkpoint_guess_config(ckpt_path, output_vae=output_vae, output_clip=output_clip, output_clipvision=False, embedding_directory=embedding_directory, output_model=True)
    #TODO: this function is a mess and should be removed eventually
    if config is None:
        with open(config_path, 'r') as stream:
            config = yaml.safe_load(stream)
    model_config_params = config['model']['params']
    clip_config = model_config_params['cond_stage_config']
    scale_factor = model_config_params['scale_factor']

    if "parameterization" in model_config_params:
        if model_config_params["parameterization"] == "v":
            m = model.clone()
            class ModelSamplingAdvanced(ldm_patched.modules.model_sampling.ModelSamplingDiscrete, ldm_patched.modules.model_sampling.V_PREDICTION):
                pass
            m.add_object_patch("model_sampling", ModelSamplingAdvanced(model.model.model_config))
            model = m

    layer_idx = clip_config.get("params", {}).get("layer_idx", None)
    if layer_idx is not None:
        clip.clip_layer(layer_idx)

    return (model, clip, vae)

def load_checkpoint_guess_config(ckpt_path, output_vae=True, output_clip=True, output_clipvision=False, embedding_directory=None, output_model=True, model_options={}, te_model_options={}):
    sd = ldm_patched.modules.utils.load_torch_file(ckpt_path)
    out = load_state_dict_guess_config(sd, output_vae, output_clip, output_clipvision, embedding_directory, output_model, model_options, te_model_options=te_model_options)
    if out is None:
        raise RuntimeError("ERROR: Could not detect model type of: {}".format(ckpt_path))
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
    if weight_dtype is not None:
        unet_weight_dtype.append(weight_dtype)

    model_config.custom_operations = model_options.get("custom_operations", model_config.custom_operations)
    unet_dtype = model_options.get("weight_dtype", None)

    if unet_dtype is None:
        unet_dtype = model_management.unet_dtype(model_params=parameters, supported_dtypes=unet_weight_dtype)

    manual_cast_dtype = model_management.unet_manual_cast(unet_dtype, load_device, model_config.supported_inference_dtypes)
    model_config.set_inference_dtype(unet_dtype, manual_cast_dtype)

    if model_config.clip_vision_prefix is not None:
        if output_clipvision:
            clipvision = clip_vision.load_clipvision_from_sd(sd, model_config.clip_vision_prefix, True)

    if output_model:
        inital_load_device = model_management.unet_inital_load_device(parameters, unet_dtype)
        offload_device = model_management.unet_offload_device()
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
        model_patcher = ldm_patched.modules.model_patcher.ModelPatcher(model, load_device=load_device, offload_device=model_management.unet_offload_device())
        if inital_load_device != torch.device("cpu"):
            logging.info("loaded straight to GPU")
            model_management.load_models_gpu([model_patcher], force_full_load=True)

    return (model_patcher, clip, vae, clipvision)


def load_diffusion_model_state_dict(sd, model_options={}): #load unet in diffusers or regular format
    dtype = model_options.get("dtype", None)

    #Allow loading unets from checkpoint files
    diffusion_model_prefix = model_detection.unet_prefix_from_state_dict(sd)
    temp_sd = ldm_patched.modules.utils.state_dict_prefix_replace(sd, {diffusion_model_prefix: ""}, filter_keys=True)
    if len(temp_sd) > 0:
        sd = temp_sd

    parameters = ldm_patched.modules.utils.calculate_parameters(sd)
    load_device = model_management.get_torch_device()
    model_config = model_detection.model_config_from_unet(sd, "")

    if model_config is not None:
        new_sd = sd
    else:
        new_sd = model_detection.convert_diffusers_mmdit(sd, "")
        if new_sd is not None: #diffusers mmdit
            model_config = model_detection.model_config_from_unet(new_sd, "")
            if model_config is None:
                return None
        else: #diffusers unet
            model_config = model_detection.model_config_from_diffusers_unet(sd)
            if model_config is None:
                return None

            diffusers_keys = ldm_patched.modules.utils.unet_to_diffusers(model_config.unet_config)

            new_sd = {}
            for k in diffusers_keys:
                if k in sd:
                    new_sd[diffusers_keys[k]] = sd.pop(k)
                else:
                    logging.warning("{} {}".format(diffusers_keys[k], k))

    offload_device = model_management.unet_offload_device()
    if dtype is None:
        unet_dtype = model_management.unet_dtype(model_params=parameters, supported_dtypes=model_config.supported_inference_dtypes)
    else:
        unet_dtype = dtype

    manual_cast_dtype = model_management.unet_manual_cast(unet_dtype, load_device, model_config.supported_inference_dtypes)
    model_config.custom_operations = model_options.get("custom_operations", model_config.custom_operations)
    model = model_config.get_model(new_sd, "")
    model = model.to(offload_device)
    model.load_model_weights(new_sd, "")
    left_over = sd.keys()
    if len(left_over) > 0:
        logging.info("left over keys in unet: {}".format(left_over))
    return ldm_patched.modules.model_patcher.ModelPatcher(model, load_device=load_device, offload_device=offload_device)

def load_diffusion_model(unet_path, model_options={}):
    sd = ldm_patched.modules.utils.load_torch_file(unet_path)
    model = load_diffusion_model_state_dict(sd, model_options=model_options)
    if model is None:
        logging.error("ERROR UNSUPPORTED UNET {}".format(unet_path))
        raise RuntimeError("ERROR: Could not detect model type of: {}".format(unet_path))
    return model

def load_unet(unet_path, dtype=None):
    print("WARNING: the load_unet function has been deprecated and will be removed please switch to: load_diffusion_model")
    return load_diffusion_model(unet_path, model_options={"dtype": dtype})

def load_unet_state_dict(sd, dtype=None):
    print("WARNING: the load_unet_state_dict function has been deprecated and will be removed please switch to: load_diffusion_model_state_dict")
    return load_diffusion_model_state_dict(sd, model_options={"dtype": dtype})


def save_checkpoint(output_path, model, clip=None, vae=None, clip_vision=None, metadata=None, extra_keys={}):
    clip_sd = None
    load_models = [model]
    if clip is not None:
        load_models.append(clip.load_model())
        clip_sd = clip.get_sd()
    vae_sd = None
    if vae is not None:
        vae_sd = vae.get_sd()

    model_management.load_models_gpu(load_models, force_patch_weights=True)
    clip_vision_sd = clip_vision.get_sd() if clip_vision is not None else None
    sd = model.model.state_dict_for_saving(clip_sd, vae_sd, clip_vision_sd)
    for k in extra_keys:
        sd[k] = extra_keys[k]

    for k in sd:
        t = sd[k]
        if not t.is_contiguous():
            sd[k] = t.contiguous()

    ldm_patched.modules.utils.save_torch_file(sd, output_path, metadata=metadata)
