import inspect
from typing import Union, Optional, Callable, List, Any
import numpy as np
import torch
import diffusers
from diffusers.pipelines.onnx_utils import ORT_TO_NP_TYPE
from diffusers.pipelines.stable_diffusion import StableDiffusionPipelineOutput
from modules.onnx_impl.pipelines import CallablePipelineBase
from modules.onnx_impl.pipelines.utils import prepare_latents


class OnnxStableDiffusionPipeline(diffusers.OnnxStableDiffusionPipeline, CallablePipelineBase):
    __module__ = 'diffusers'
    __name__ = 'OnnxStableDiffusionPipeline'

    def __init__(
        self,
        vae_encoder: diffusers.OnnxRuntimeModel,
        vae_decoder: diffusers.OnnxRuntimeModel,
        text_encoder: diffusers.OnnxRuntimeModel,
        tokenizer: Any,
        unet: diffusers.OnnxRuntimeModel,
        scheduler: Any,
        safety_checker: diffusers.OnnxRuntimeModel,
        feature_extractor: Any,
        requires_safety_checker: bool = True
    ):
        super().__init__(vae_encoder, vae_decoder, text_encoder, tokenizer, unet, scheduler, safety_checker, feature_extractor, requires_safety_checker)

    def __call__(
        self,
        prompt: Union[str, List[str]] = None,
        height: Optional[int] = 512,
        width: Optional[int] = 512,
        num_inference_steps: Optional[int] = 50,
        guidance_scale: Optional[float] = 7.5,
        negative_prompt: Optional[Union[str, List[str]]] = None,
        num_images_per_prompt: Optional[int] = 1,
        eta: Optional[float] = 0.0,
        generator: Optional[Union[torch.Generator, List[torch.Generator]]] = None,
        latents: Optional[np.ndarray] = None,
        prompt_embeds: Optional[np.ndarray] = None,
        negative_prompt_embeds: Optional[np.ndarray] = None,
        output_type: Optional[str] = "pil",
        return_dict: bool = True,
        callback: Optional[Callable[[int, int, np.ndarray], None]] = None,
        callback_steps: int = 1,
    ):
        # check inputs. Raise error if not correct
        self.check_inputs(
            prompt, height, width, callback_steps, negative_prompt, prompt_embeds, negative_prompt_embeds
        )

        # define call parameters
        if prompt is not None and isinstance(prompt, str):
            batch_size = 1
        elif prompt is not None and isinstance(prompt, list):
            batch_size = len(prompt)
        else:
            batch_size = prompt_embeds.shape[0]

        if generator is None:
            generator = torch.Generator("cpu")

        # set timesteps
        self.scheduler.set_timesteps(num_inference_steps)

        # here `guidance_scale` is defined analog to the guidance weight `w` of equation (2)
        # of the Imagen paper: https://arxiv.org/pdf/2205.11487.pdf . `guidance_scale = 1`
        # corresponds to doing no classifier free guidance.
        do_classifier_free_guidance = guidance_scale > 1.0

        prompt_embeds = self._encode_prompt(
            prompt,
            num_images_per_prompt,
            do_classifier_free_guidance,
            negative_prompt,
            prompt_embeds=prompt_embeds,
            negative_prompt_embeds=negative_prompt_embeds,
        )

        # get the initial random noise unless the user supplied it
        latents = prepare_latents(
            self.scheduler.init_noise_sigma,
            batch_size * num_images_per_prompt,
            height,
            width,
            prompt_embeds.dtype,
            generator,
            latents,
        )

        # prepare extra kwargs for the scheduler step, since not all schedulers have the same signature
        # eta (η) is only used with the DDIMScheduler, it will be ignored for other schedulers.
        # eta corresponds to η in DDIM paper: https://arxiv.org/abs/2010.02502
        # and should be between [0, 1]
        accepts_eta = "eta" in set(inspect.signature(self.scheduler.step).parameters.keys())
        extra_step_kwargs = {}
        if accepts_eta:
            extra_step_kwargs["eta"] = eta

        timestep_dtype = next(
            (input.type for input in self.unet.model.get_inputs() if input.name == "timestep"), "tensor(float)"
        )
        timestep_dtype = ORT_TO_NP_TYPE[timestep_dtype]

        for i, t in enumerate(self.progress_bar(self.scheduler.timesteps)):
            # expand the latents if we are doing classifier free guidance
            latent_model_input = np.concatenate([latents] * 2) if do_classifier_free_guidance else latents
            latent_model_input = self.scheduler.scale_model_input(torch.from_numpy(latent_model_input), t)
            latent_model_input = latent_model_input.cpu().numpy()

            # predict the noise residual
            timestep = np.array([t], dtype=timestep_dtype)
            noise_pred = self.unet(sample=latent_model_input, timestep=timestep, encoder_hidden_states=prompt_embeds)
            noise_pred = noise_pred[0]

            # perform guidance
            if do_classifier_free_guidance:
                noise_pred_uncond, noise_pred_text = np.split(noise_pred, 2)
                noise_pred = noise_pred_uncond + guidance_scale * (noise_pred_text - noise_pred_uncond)

            # compute the previous noisy sample x_t -> x_t-1
            scheduler_output = self.scheduler.step(
                torch.from_numpy(noise_pred), t, torch.from_numpy(latents), **extra_step_kwargs
            )
            latents = scheduler_output.prev_sample.numpy()

            # call the callback, if provided
            if callback is not None and i % callback_steps == 0:
                step_idx = i // getattr(self.scheduler, "order", 1)
                callback(step_idx, t, latents)

        has_nsfw_concept = None

        if output_type != "latent":
            latents /= self.vae_decoder.config.get("scaling_factor", 0.18215)

            # image = self.vae_decoder(latent_sample=latents)[0]
            # it seems likes there is a strange result for using half-precision vae decoder if batchsize>1
            image = np.concatenate(
                [self.vae_decoder(latent_sample=latents[i : i + 1])[0] for i in range(latents.shape[0])]
            )

            image = np.clip(image / 2 + 0.5, 0, 1)
            image = image.transpose((0, 2, 3, 1))

            if self.safety_checker is not None:
                safety_checker_input = self.feature_extractor(
                    self.numpy_to_pil(image), return_tensors="np"
                ).pixel_values.astype(image.dtype)

                images, has_nsfw_concept = [], []
                for i in range(image.shape[0]):
                    image_i, has_nsfw_concept_i = self.safety_checker(
                        clip_input=safety_checker_input[i : i + 1], images=image[i : i + 1]
                    )
                    images.append(image_i)
                    has_nsfw_concept.append(has_nsfw_concept_i[0])
                image = np.concatenate(images)

            if output_type == "pil":
                image = self.numpy_to_pil(image)
        else:
            image = latents

        if not return_dict:
            return (image, has_nsfw_concept)

        return StableDiffusionPipelineOutput(images=image, nsfw_content_detected=has_nsfw_concept)
