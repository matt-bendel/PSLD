"""SAMPLING ONLY."""

import torch
import numpy as np
from tqdm import tqdm
from functools import partial
from torch.optim.adam import Adam

from ldm.modules.diffusionmodules.util import make_ddim_sampling_parameters, make_ddim_timesteps, noise_like, \
    extract_into_tensor

import pdb

class DDIMSampler(object):
    def __init__(self, model, schedule="linear", **kwargs):
        super().__init__()
        self.model = model
        self.ddpm_num_timesteps = model.num_timesteps
        self.schedule = schedule
        # TODO
        self.optimal_c = None
        self.opt = None
        self.K = 5

    def register_buffer(self, name, attr):
        if type(attr) == torch.Tensor:
            if attr.device != torch.device("cuda"):
                attr = attr.to(torch.device("cuda"))
        setattr(self, name, attr)

    def make_schedule(self, ddim_num_steps, ddim_discretize="uniform", ddim_eta=0., verbose=True):
        self.ddim_timesteps = make_ddim_timesteps(ddim_discr_method=ddim_discretize, num_ddim_timesteps=ddim_num_steps,
                                                  num_ddpm_timesteps=self.ddpm_num_timesteps,verbose=verbose)
        
        alphas_cumprod = self.model.alphas_cumprod
        assert alphas_cumprod.shape[0] == self.ddpm_num_timesteps, 'alphas have to be defined for each timestep'
        to_torch = lambda x: x.clone().detach().to(torch.float32).to(self.model.device)

        self.register_buffer('betas', to_torch(self.model.betas))
        self.register_buffer('alphas_cumprod', to_torch(alphas_cumprod))
        self.register_buffer('alphas_cumprod_prev', to_torch(self.model.alphas_cumprod_prev))

        # calculations for diffusion q(x_t | x_{t-1}) and others
        self.register_buffer('sqrt_alphas_cumprod', to_torch(np.sqrt(alphas_cumprod.cpu())))
        self.register_buffer('sqrt_one_minus_alphas_cumprod', to_torch(np.sqrt(1. - alphas_cumprod.cpu())))
        self.register_buffer('log_one_minus_alphas_cumprod', to_torch(np.log(1. - alphas_cumprod.cpu())))
        self.register_buffer('sqrt_recip_alphas_cumprod', to_torch(np.sqrt(1. / alphas_cumprod.cpu())))
        self.register_buffer('sqrt_recipm1_alphas_cumprod', to_torch(np.sqrt(1. / alphas_cumprod.cpu() - 1)))

        # ddim sampling parameters
        ddim_sigmas, ddim_alphas, ddim_alphas_prev = make_ddim_sampling_parameters(alphacums=alphas_cumprod.cpu(),
                                                                                   ddim_timesteps=self.ddim_timesteps,
                                                                                   eta=ddim_eta,verbose=verbose)
        self.register_buffer('ddim_sigmas', ddim_sigmas)
        self.register_buffer('ddim_alphas', ddim_alphas)
        self.register_buffer('ddim_alphas_prev', ddim_alphas_prev)
        self.register_buffer('ddim_sqrt_one_minus_alphas', np.sqrt(1. - ddim_alphas))
        sigmas_for_original_sampling_steps = ddim_eta * torch.sqrt(
            (1 - self.alphas_cumprod_prev) / (1 - self.alphas_cumprod) * (
                        1 - self.alphas_cumprod / self.alphas_cumprod_prev))
        self.register_buffer('ddim_sigmas_for_original_num_steps', sigmas_for_original_sampling_steps)

    # @torch.no_grad()
    def sample(self,
               S,
               batch_size,
               shape,
               conditioning=None,
               callback=None,
               normals_sequence=None,
               img_callback=None,
               quantize_x0=False,
               eta=0.,
               mask=None,
               x0=None,
               temperature=1.,
               noise_dropout=0.,
               score_corrector=None,
               corrector_kwargs=None,
               verbose=True,
               x_T=None,
               log_every_t=100,
               unconditional_guidance_scale=1.,
               unconditional_conditioning=None,
               ip_mask = None, measurements = None, operator = None, gamma = 1, inpainting = False, omega=1,
               general_inverse = None, noiser=None,
               ffhq256=False,
               # this has to come in the same format as the conditioning, # e.g. as encoded tokens, ...
               **kwargs
               ):

        if conditioning is not None:
            if isinstance(conditioning, dict):
                cbs = conditioning[list(conditioning.keys())[0]].shape[0]
                if cbs != batch_size:
                    print(f"Warning: Got {cbs} conditionings but batch-size is {batch_size}")
            else:
                if conditioning.shape[0] != batch_size:
                    print(f"Warning: Got {conditioning.shape[0]} conditionings but batch-size is {batch_size}")
        else:
            print('Running unconditional generation...')

        # TODO
        if self.optimal_c is None:
            self.optimal_c = conditioning

        print(self.optimal_c.detach().cpu().numpy()[0])

        print(f'GUIDANCE: {unconditional_guidance_scale}')
        self.make_schedule(ddim_num_steps=S, ddim_eta=eta, verbose=verbose)
        # sampling
        C, H, W = shape
        size = (batch_size, C, H, W)
        print(f'Data shape for DDIM sampling is {size}, eta {eta}')

        samples, intermediates = self.ddim_sampling(conditioning, size,
                                                    callback=callback,
                                                    img_callback=img_callback,
                                                    quantize_denoised=quantize_x0,
                                                    mask=mask, x0=x0,
                                                    ddim_use_original_steps=False,
                                                    noise_dropout=noise_dropout,
                                                    temperature=temperature,
                                                    score_corrector=score_corrector,
                                                    corrector_kwargs=corrector_kwargs,
                                                    x_T=x_T,
                                                    log_every_t=log_every_t,
                                                    unconditional_guidance_scale=unconditional_guidance_scale,
                                                    unconditional_conditioning=unconditional_conditioning,
                                                    ip_mask = ip_mask, measurements = measurements, operator = operator,
                                                    gamma = gamma,
                                                    inpainting = inpainting, omega=omega,
                                                    general_inverse = general_inverse, noiser = noiser,
                                                    ffhq256=ffhq256
                                                    )
        print(self.optimal_c.detach().cpu().numpy()[0])

        return samples, intermediates

    ## lr
    # @torch.no_grad()
    def ddim_sampling(self, cond, shape,
                      x_T=None, ddim_use_original_steps=False,
                      callback=None, timesteps=None, quantize_denoised=False,
                      mask=None, x0=None, img_callback=None, log_every_t=100,
                      temperature=1., noise_dropout=0., score_corrector=None, corrector_kwargs=None,
                      unconditional_guidance_scale=1., unconditional_conditioning=None,
                      ip_mask = None, measurements = None, operator = None, gamma = 1, inpainting=False, omega=1,
                      general_inverse = None, noiser=None,
                      ffhq256=False):
        device = self.model.betas.device
        b = shape[0]
        if x_T is None:
            img = torch.randn(shape, device=device)
        else:
            img = x_T

        if inpainting:
            print('inpaing baby!')

        if timesteps is None:
            timesteps = self.ddpm_num_timesteps if ddim_use_original_steps else self.ddim_timesteps
        elif timesteps is not None and not ddim_use_original_steps:
            subset_end = int(min(timesteps / self.ddim_timesteps.shape[0], 1) * self.ddim_timesteps.shape[0]) - 1
            timesteps = self.ddim_timesteps[:subset_end]

        intermediates = {'x_inter': [img], 'pred_x0': [img]}
        time_range = reversed(range(0,timesteps)) if ddim_use_original_steps else np.flip(timesteps)
        total_steps = timesteps if ddim_use_original_steps else timesteps.shape[0]
        print(f"Running DDIM Sampling with {total_steps} timesteps")

        iterator = tqdm(time_range, desc='DDIM Sampler', total=total_steps)

        c_opt = True

        meas_errors = []

        for i, step in enumerate(iterator):
            index = total_steps - i - 1
            #print('index:', index)
            ts = torch.full((b,), step, device=device, dtype=torch.long)
            ts_next = torch.full((b,), step+1, device=device, dtype=torch.long)

            if mask is not None:
                assert x0 is not None
                img_orig = self.model.q_sample(x0, ts)  # TODO: deterministic forward pass?
                img = img_orig * mask + (1. - mask) * img

            outs = self.p_sample_ddim(img, cond, ts, ts_next, index=index, use_original_steps=ddim_use_original_steps,
                                      quantize_denoised=quantize_denoised, temperature=temperature,
                                      noise_dropout=noise_dropout, score_corrector=score_corrector,
                                      corrector_kwargs=corrector_kwargs,
                                      unconditional_guidance_scale=unconditional_guidance_scale,
                                      unconditional_conditioning=unconditional_conditioning,
                                      ip_mask = ip_mask, measurements = measurements, operator = operator, gamma = gamma,
                                      inpainting=inpainting, omega=omega,
                                      gamma_scale = index/total_steps,
                                      general_inverse=general_inverse, noiser=noiser,
                                      ffhq256=ffhq256,
                                      c_opt=c_opt, dc=False)
            img, pred_x0 = outs

            if callback: callback(i)
            if img_callback: img_callback(pred_x0, i)

            if index % log_every_t == 0 or index == total_steps - 1:
                intermediates['x_inter'].append(img)
                intermediates['pred_x0'].append(pred_x0)

        return img, intermediates

    ######################
    def p_sample_ddim(self, x, c, t, t_next, index, repeat_noise=False, use_original_steps=False, quantize_denoised=False,
                      temperature=1., noise_dropout=0., score_corrector=None, corrector_kwargs=None,
                      unconditional_guidance_scale=1., unconditional_conditioning=None,
                      ip_mask=None, measurements = None, operator = None, gamma=1, inpainting=False,
                      gamma_scale = None, omega = 1e-1,
                      general_inverse=False,noiser=None,
                      ffhq256=False, c_opt=False, dc=False):
        b, *_, device = *x.shape, x.device

        optimal_c = self.optimal_c
        optimal_c.requires_grad = True
           
        ##########################################
        ## measurment consistency guided diffusion
        ##########################################
        if inpainting:
            z_t = torch.clone(x.detach())
            z_t.requires_grad = True

            if unconditional_conditioning is None or unconditional_guidance_scale == 1.:
                e_t = self.model.apply_model(z_t, t, optimal_c.detach())
            else:
                x_in = torch.cat([z_t] * 2)
                t_in = torch.cat([t] * 2)
                c_in = torch.cat([unconditional_conditioning, optimal_c.detach()])
                e_t_uncond, e_t = self.model.apply_model(x_in, t_in, c_in).chunk(2)
                e_t = e_t_uncond + unconditional_guidance_scale * (e_t - e_t_uncond)


            if score_corrector is not None:
                assert self.model.parameterization == "eps"
                e_t = score_corrector.modify_score(self.model, e_t, z_t, t, c, **corrector_kwargs)
            
            
            alphas = self.model.alphas_cumprod if use_original_steps else self.ddim_alphas
            alphas_prev = self.model.alphas_cumprod_prev if use_original_steps else self.ddim_alphas_prev
            sqrt_one_minus_alphas = self.model.sqrt_one_minus_alphas_cumprod if use_original_steps else self.ddim_sqrt_one_minus_alphas
            sigmas = self.model.ddim_sigmas_for_original_num_steps if use_original_steps else self.ddim_sigmas
            # select parameters corresponding to the currently considered timestep
            a_t = torch.full((b, 1, 1, 1), alphas[index], device=device)
            a_prev = torch.full((b, 1, 1, 1), alphas_prev[index], device=device)
            sigma_t = torch.full((b, 1, 1, 1), sigmas[index], device=device)
            sqrt_one_minus_at = torch.full((b, 1, 1, 1), sqrt_one_minus_alphas[index],device=device)
            
            # current prediction for x_0
            pred_z_0 = (z_t - sqrt_one_minus_at * e_t) / a_t.sqrt()

            #######
            
            if quantize_denoised:
                pred_z_0, _, *_ = self.model.first_stage_model.quantize(pred_z_0)
            
            
            # direction pointing to x_t
            dir_zt = (1. - a_prev - sigma_t**2).sqrt() * e_t
            noise = sigma_t * noise_like(x.shape, device, repeat_noise) * temperature
            if noise_dropout > 0.:
                noise = torch.nn.functional.dropout(noise, p=noise_dropout)

            z_prev = a_prev.sqrt() * pred_z_0 + dir_zt + noise

            for k in range(self.K):
                if index == 0:
                    break

                if unconditional_conditioning is None or unconditional_guidance_scale == 1.:
                    e_t = self.model.apply_model(z_prev, t_next, optimal_c)
                else:
                    # 2 NFEs, No good!!
                    with torch.no_grad():
                        e_t_uncond = self.model.apply_model(z_prev, t_next, unconditional_conditioning)

                    e_t = self.model.apply_model(z_prev, t_next, optimal_c)

                    e_t = e_t_uncond + unconditional_guidance_scale * (e_t - e_t_uncond)

                if score_corrector is not None:
                    assert self.model.parameterization == "eps"
                    e_t = score_corrector.modify_score(self.model, e_t, z_prev, t_next, c, **corrector_kwargs)

                alphas_prev = self.model.alphas_cumprod_prev if use_original_steps else self.ddim_alphas_prev
                a_prev = torch.full((b, 1, 1, 1), alphas_prev[index], device=device)

                # current prediction for x_0
                pred_prev_z_0 = (z_prev - (1 - a_prev).sqrt() * e_t) / a_prev.sqrt()

                if quantize_denoised:
                    pred_prev_z_0, _, *_ = self.model.first_stage_model.quantize(pred_prev_z_0)

                image_pred = self.model.differentiable_decode_first_stage(pred_prev_z_0)
                meas_pred = operator.forward(image_pred, mask=ip_mask)
                meas_pred = noiser(meas_pred)
                meas_error = torch.linalg.norm(meas_pred - measurements)

                ortho_project = image_pred - operator.transpose(operator.forward(image_pred, mask=ip_mask))
                parallel_project = operator.transpose(measurements)
                inpainted_image = parallel_project + ortho_project

                # pdb.set_trace()
                # encoded_z_0 = self.model.encode_first_stage(inpainted_image) if ffhq256 else self.model.encode_first_stage(inpainted_image)
                encoded_z_0 = self.model.encode_first_stage(inpainted_image.type(torch.float32))
                encoded_z_0 = self.model.get_first_stage_encoding(encoded_z_0)
                inpaint_error = torch.linalg.norm(encoded_z_0 - pred_prev_z_0)

                error = inpaint_error * gamma #+ meas_error * omega

                gradients = torch.autograd.grad(error, inputs=optimal_c)[0]
                optimal_c = optimal_c - gradients

                print(f'TEXT LOSS: {error.item()}')

            self.optimal_c = optimal_c.detach()

            # return z_prev.detach(), pred_z_0.detach()

            
            ##############################################
            image_pred = self.model.differentiable_decode_first_stage(pred_z_0)
            meas_pred = operator.forward(image_pred, mask=ip_mask)
            meas_pred = noiser(meas_pred)
            meas_error = torch.linalg.norm(meas_pred - measurements)

            # ortho_project = image_pred - operator.transpose(operator.forward(image_pred, mask=ip_mask))
            # parallel_project = operator.transpose(measurements)
            # inpainted_image = parallel_project + ortho_project

            # pdb.set_trace()
            # encoded_z_0 = self.model.encode_first_stage(inpainted_image) if ffhq256 else self.model.encode_first_stage(inpainted_image)
            # encoded_z_0 = self.model.encode_first_stage(inpainted_image.type(torch.float32))
            # encoded_z_0 = self.model.get_first_stage_encoding(encoded_z_0)
            # inpaint_error = torch.linalg.norm(encoded_z_0 - pred_z_0)

            # error = inpaint_error * gamma + meas_error * omega
            error = omega * meas_error

            gradients = torch.autograd.grad(error, inputs=z_t)[0]
            z_prev = z_prev - gradients
            print('Loss: ', error.item())
            
            return z_prev.detach(), pred_z_0.detach()
        
        elif general_inverse:
            print('Running general inverse module...')
            z_t = torch.clone(x.detach())
            z_t.requires_grad = True
            
            if unconditional_conditioning is None or unconditional_guidance_scale == 1.:
                e_t = self.model.apply_model(z_t, t, c)
            else:
                x_in = torch.cat([z_t] * 2)
                t_in = torch.cat([t] * 2)
                c_in = torch.cat([unconditional_conditioning, c])
                e_t_uncond, e_t = self.model.apply_model(x_in, t_in, c_in).chunk(2)
                e_t = e_t_uncond + unconditional_guidance_scale * (e_t - e_t_uncond)
            
            
            if score_corrector is not None:
                assert self.model.parameterization == "eps"
                e_t = score_corrector.modify_score(self.model, e_t, z_t, t, c, **corrector_kwargs)
            
            
            alphas = self.model.alphas_cumprod if use_original_steps else self.ddim_alphas
            alphas_prev = self.model.alphas_cumprod_prev if use_original_steps else self.ddim_alphas_prev
            sqrt_one_minus_alphas = self.model.sqrt_one_minus_alphas_cumprod if use_original_steps else self.ddim_sqrt_one_minus_alphas
            sigmas = self.model.ddim_sigmas_for_original_num_steps if use_original_steps else self.ddim_sigmas
            # select parameters corresponding to the currently considered timestep
            a_t = torch.full((b, 1, 1, 1), alphas[index], device=device)
            a_prev = torch.full((b, 1, 1, 1), alphas_prev[index], device=device)
            sigma_t = torch.full((b, 1, 1, 1), sigmas[index], device=device)
            sqrt_one_minus_at = torch.full((b, 1, 1, 1), sqrt_one_minus_alphas[index],device=device)
            
            # current prediction for x_0
            pred_z_0 = (z_t - sqrt_one_minus_at * e_t) / a_t.sqrt()
            
            
            if quantize_denoised:
                pred_z_0, _, *_ = self.model.first_stage_model.quantize(pred_z_0)
            
            
            # direction pointing to x_t
            dir_zt = (1. - a_prev - sigma_t**2).sqrt() * e_t
            noise = sigma_t * noise_like(x.shape, device, repeat_noise) * temperature
            if noise_dropout > 0.:
                noise = torch.nn.functional.dropout(noise, p=noise_dropout)
            
            z_prev = a_prev.sqrt() * pred_z_0 + dir_zt + noise
            
            
            ##############################################
            image_pred = self.model.differentiable_decode_first_stage(pred_z_0)
            meas_pred = operator.forward(image_pred)
            meas_pred = noiser(meas_pred)
            meas_error = torch.linalg.norm(meas_pred - measurements)
            
            ortho_project = image_pred - operator.transpose(operator.forward(image_pred))
            parallel_project = operator.transpose(measurements)
            inpainted_image = parallel_project + ortho_project
            
            # encoded_z_0 = self.model.encode_first_stage(inpainted_image) if ffhq256 else self.model.encode_first_stage(inpainted_image).mean  
            # encoded_z_0 = self.model.encode_first_stage(inpainted_image)
            # encoded_z_0 = self.model.get_first_stage_encoding(encoded_z_0)
            # inpaint_error = torch.linalg.norm(encoded_z_0 - pred_z_0)
            
            # error = inpaint_error * gamma + meas_error * omega
            error = meas_error * omega

            gradients = torch.autograd.grad(error, inputs=z_t)[0]
            z_prev = z_prev - gradients
            print('Loss: ', error.item())
            
            return z_prev.detach(), pred_z_0.detach()
        
        
        #########################################
        else:
            if unconditional_conditioning is None or unconditional_guidance_scale == 1.:
                with torch.no_grad():
                    e_t = self.model.apply_model(x, t, c)
            else:
                x_in = torch.cat([x] * 2)
                t_in = torch.cat([t] * 2)
                c_in = torch.cat([unconditional_conditioning, c])
                ## lr
                with torch.no_grad():
                    e_t_uncond, e_t = self.model.apply_model(x_in, t_in, c_in).chunk(2)
                e_t = e_t_uncond + unconditional_guidance_scale * (e_t - e_t_uncond)

            if score_corrector is not None:
                assert self.model.parameterization == "eps"
                ## lr
                with torch.no_grad():
                    e_t = score_corrector.modify_score(self.model, e_t, x, t, c, **corrector_kwargs)

            alphas = self.model.alphas_cumprod if use_original_steps else self.ddim_alphas
            alphas_prev = self.model.alphas_cumprod_prev if use_original_steps else self.ddim_alphas_prev
            sqrt_one_minus_alphas = self.model.sqrt_one_minus_alphas_cumprod if use_original_steps else self.ddim_sqrt_one_minus_alphas
            sigmas = self.model.ddim_sigmas_for_original_num_steps if use_original_steps else self.ddim_sigmas
            # select parameters corresponding to the currently considered timestep
            a_t = torch.full((b, 1, 1, 1), alphas[index], device=device)
            a_prev = torch.full((b, 1, 1, 1), alphas_prev[index], device=device)
            sigma_t = torch.full((b, 1, 1, 1), sigmas[index], device=device)
            sqrt_one_minus_at = torch.full((b, 1, 1, 1), sqrt_one_minus_alphas[index],device=device)

            # current prediction for x_0
            pred_x0 = (x - sqrt_one_minus_at * e_t) / a_t.sqrt()
            if quantize_denoised:
                ## 
                with torch.no_grad():
                    pred_x0, _, *_ = self.model.first_stage_model.quantize(pred_x0)
            # direction pointing to x_t
            dir_xt = (1. - a_prev - sigma_t**2).sqrt() * e_t
            noise = sigma_t * noise_like(x.shape, device, repeat_noise) * temperature
            if noise_dropout > 0.:
                noise = torch.nn.functional.dropout(noise, p=noise_dropout)
            x_prev = a_prev.sqrt() * pred_x0 + dir_xt + noise

            return x_prev, pred_x0
    
    ######################
    
    #@torch.no_grad()
    def stochastic_encode(self, x0, t, use_original_steps=False, noise=None):
        # fast, but does not allow for exact reconstruction
        # t serves as an index to gather the correct alphas
        if use_original_steps:
            sqrt_alphas_cumprod = self.sqrt_alphas_cumprod
            sqrt_one_minus_alphas_cumprod = self.sqrt_one_minus_alphas_cumprod
        else:
            sqrt_alphas_cumprod = torch.sqrt(self.ddim_alphas)
            sqrt_one_minus_alphas_cumprod = self.ddim_sqrt_one_minus_alphas

        if noise is None:
            noise = torch.randn_like(x0)
        return (extract_into_tensor(sqrt_alphas_cumprod, t, x0.shape) * x0 +
                extract_into_tensor(sqrt_one_minus_alphas_cumprod, t, x0.shape) * noise)

    #@torch.no_grad()
    def decode(self, x_latent, cond, t_start, unconditional_guidance_scale=1.0, unconditional_conditioning=None,
               use_original_steps=False):

        timesteps = np.arange(self.ddpm_num_timesteps) if use_original_steps else self.ddim_timesteps
        timesteps = timesteps[:t_start]

        time_range = np.flip(timesteps)
        total_steps = timesteps.shape[0]
        print(f"Running DDIM Sampling with {total_steps} timesteps")

        iterator = tqdm(time_range, desc='Decoding image', total=total_steps)
        x_dec = x_latent
        for i, step in enumerate(iterator):
            index = total_steps - i - 1
            ts = torch.full((x_latent.shape[0],), step, device=x_latent.device, dtype=torch.long)
            x_dec, _ = self.p_sample_ddim(x_dec, cond, ts, index=index, use_original_steps=use_original_steps,
                                          unconditional_guidance_scale=unconditional_guidance_scale,
                                          unconditional_conditioning=unconditional_conditioning)
        return x_dec