# ThermoDigitalTwin

`ThermoDigitalTwin` is the simulated version of the `ThermoMicroscope`.  
It provides realistic-enough image and spectrum behavior for development, testing, and demos without requiring AutoScript or hardware.

## How it works

1. On startup, the twin generates a **persistent synthetic sample** (deterministic from seed).
2. Stage pose (`x, y, z, alpha, beta`) defines the current viewport into that sample.
3. `get_scanned_image()` renders an image from the current pose and FoV.
4. `get_spectrum("eds")` estimates composition at the current beam position using the same projected sample state.

This means moving the stage navigates the sample, and revisiting the same pose can reproduce the same view when stage noise is disabled.

## Available features

- Persistent sample per device session
- Deterministic sample generation via seed
- Stage-coupled navigation in **XY + Z + alpha/beta tilt**
- Beam-position-dependent spectrum simulation
- Configurable stage move noise
- Viewport metadata reporting
- Manual sample regeneration with a new seed

## Key properties

- `sample_seed`: controls deterministic sample generation
- `sample_particle_count`: controls synthetic particle count
- `sample_extent_scale`: controls sample XY size relative to FoV
- `stage_move_noise_std`: adds Gaussian perturbation to stage moves

## Key commands

- `move_stage([x, y, z, alpha, beta])`
- `get_stage()`
- `set_fov(fov)`
- `get_scanned_image()`
- `place_beam([x, y])`
- `get_spectrum("eds")`
- `get_viewport_metadata()`
- `regenerate_sample(seed)`