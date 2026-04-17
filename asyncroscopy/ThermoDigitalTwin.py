"""
Digital twin version of ThermoMicroscope for HAADF-EDX.

Useful for testing and development without requiring AutoScript hardware.
"""

import json

import numpy as np
import pyTEMlib.image_tools as it
import pyTEMlib.probe_tools as pt
import tango
from ase import Atoms
from ase.build import bulk
from tango import AttrWriteType, DevState
from tango.server import Device, attribute, command, device_property

from asyncroscopy.Microscope import Microscope


class ThermoDigitalTwin(Microscope):
    """
    Persistent ASE-backed sample simulation with stage-coupled viewport rendering.
    """

    sample_seed = device_property(
        dtype=int,
        default_value=12345,
        doc="Seed used to generate deterministic sample geometry.",
    )
    sample_particle_count = device_property(
        dtype=int,
        default_value=40,
        doc="Number of particles in the generated sample.",
    )
    sample_extent_scale = device_property(
        dtype=float,
        default_value=3.0,
        doc="Sample XY extent as a multiple of current FoV.",
    )
    stage_move_noise_std = device_property(
        dtype=float,
        default_value=0.0,
        doc="Gaussian move noise standard deviation in meters (applied to x,y,z).",
    )

    manufacturer = attribute(
        label="ThermoDigitalTwin",
        dtype=str,
        doc="Simulation backend",
    )

    beam_pos = attribute(
        label="Beam Position",
        dtype=(float,),
        max_dim_x=2,
        access=AttrWriteType.READ_WRITE,
        unit="fractional",
        min_value=0.0,
        max_value=1.0,
        doc="Beam position as [x, y] fractional coordinates, each in range [0.0, 1.0]",
    )

    def init_device(self) -> None:
        Device.init_device(self)
        self.set_state(DevState.INIT)

        self._stem_mode = True
        self._detector_proxies: dict[str, tango.DeviceProxy] = {}
        self._manufacturer = "UTKTeam"
        self._beam_pos_x = 0.5
        self._beam_pos_y = 0.5
        self._imsize = 512
        self._fov = 200e-10  # meters
        self._stage_position = np.zeros(5, dtype=np.float64)  # x, y, z, alpha, beta

        self._sample_seed_runtime = int(self.sample_seed)
        self._sample_atoms_base = Atoms()
        self._sample_atoms_view = Atoms()
        self._particle_records_base: list[dict] = []
        self._particle_records_view: list[dict] = []
        self._world_bounds_ang = {
            "x_min": -1.0,
            "x_max": 1.0,
            "y_min": -1.0,
            "y_max": 1.0,
            "z_min": -1.0,
            "z_max": 1.0,
        }
        self._cached_pose_key: tuple | None = None
        self._all_sample_elements: list[str] = []

        self._connect()

    def _connect(self):
        """Simulate connection by connecting to detector proxies."""
        self._connect_detector_proxies()
        self._generate_sample(seed=self._sample_seed_runtime)
        self.set_state(DevState.ON)

    def _connect_detector_proxies(self) -> None:
        """Build DeviceProxy objects for each configured detector device."""
        addresses: dict[str, str] = {
            "eds": self.eds_device_address,
            "stage": self.stage_device_address,
            "scan": self.scan_device_address,
        }
        for name, address in addresses.items():
            if not address:
                self.info_stream(f"Skipping {name}: no address configured")
                continue
            try:
                self._detector_proxies[name] = tango.DeviceProxy(address)
                self.info_stream(f"Connected to detector proxy: {name} @ {address}")
            except tango.DevFailed as e:
                self.error_stream(f"Failed to connect to {name} proxy at {address}: {e}")

    def _sync_stage_from_proxy(self) -> None:
        stage = self._detector_proxies.get("stage")
        if stage is None:
            return
        try:
            self._stage_position = np.array(
                [stage.x, stage.y, stage.z, stage.alpha, stage.beta],
                dtype=np.float64,
            )
            self._update_view_cache(force=False)
        except tango.DevFailed:
            self.error_stream("Failed to read stage proxy position; using internal stage state.")

    @staticmethod
    def _rotation_matrix_from_stage(alpha_deg: float, beta_deg: float) -> np.ndarray:
        a = np.radians(alpha_deg)
        b = np.radians(beta_deg)
        rx = np.array(
            [
                [1.0, 0.0, 0.0],
                [0.0, np.cos(a), -np.sin(a)],
                [0.0, np.sin(a), np.cos(a)],
            ]
        )
        ry = np.array(
            [
                [np.cos(b), 0.0, np.sin(b)],
                [0.0, 1.0, 0.0],
                [-np.sin(b), 0.0, np.cos(b)],
            ]
        )
        return ry @ rx

    @staticmethod
    def _rotation_matrix_zyx(alpha: float, beta: float, gamma: float) -> np.ndarray:
        a, b, g = np.radians([alpha, beta, gamma])
        rz = np.array([[np.cos(a), -np.sin(a), 0], [np.sin(a), np.cos(a), 0], [0, 0, 1]])
        ry = np.array([[np.cos(b), 0, np.sin(b)], [0, 1, 0], [-np.sin(b), 0, np.cos(b)]])
        rx = np.array([[1, 0, 0], [0, np.cos(g), -np.sin(g)], [0, np.sin(g), np.cos(g)]])
        return rz @ ry @ rx

    def _transform_positions_by_stage(self, positions: np.ndarray) -> np.ndarray:
        stage_xyz_ang = self._stage_position[:3] * 1e10
        alpha_deg, beta_deg = self._stage_position[3], self._stage_position[4]
        rot = self._rotation_matrix_from_stage(alpha_deg, beta_deg)
        center = np.zeros(3, dtype=np.float64)
        rotated = (positions - center) @ rot.T + center
        return rotated - stage_xyz_ang

    def _update_view_cache(self, force: bool = False) -> None:
        pose_key = tuple(np.round(self._stage_position, 12))
        if not force and pose_key == self._cached_pose_key:
            return

        if len(self._sample_atoms_base) > 0:
            base_positions = self._sample_atoms_base.get_positions()
            transformed = self._transform_positions_by_stage(base_positions)
            self._sample_atoms_view = Atoms(
                symbols=self._sample_atoms_base.get_chemical_symbols(),
                positions=transformed,
            )
        else:
            self._sample_atoms_view = Atoms()

        self._particle_records_view = []
        for rec in self._particle_records_base:
            center_view = self._transform_positions_by_stage(rec["center"].reshape(1, 3))[0]
            self._particle_records_view.append(
                {
                    "center": center_view,
                    "radius": rec["radius"],
                    "btype": rec["btype"],
                    "composition": rec["composition"],
                }
            )
        self._cached_pose_key = pose_key

    @staticmethod
    def _sub_pix_gaussian(size: int = 11, sigma: float = 0.8, dx: float = 0.0, dy: float = 0.0) -> np.ndarray:
        coords = np.arange(size) - (size - 1) / 2.0
        xx, yy = np.meshgrid(coords, coords)
        g = np.exp(-(((xx + dx) ** 2 + (yy + dy) ** 2) / (2 * sigma**2)))
        m = np.max(g)
        return g / m if m > 0 else g

    def _create_pseudo_potential(
        self,
        xtal: Atoms,
        pixel_size: float,
        sigma: float,
        bounds: tuple[float, float, float, float],
        atom_frame: int = 11,
    ) -> np.ndarray:
        x_min, x_max, y_min, y_max = bounds
        pixels_x = int(np.round((x_max - x_min) / pixel_size))
        pixels_y = int(np.round((y_max - y_min) / pixel_size))
        potential_map = np.zeros((pixels_x, pixels_y), dtype=np.float32)
        padding = atom_frame
        padded = np.pad(potential_map, padding, mode="constant", constant_values=0.0)

        if len(xtal) == 0:
            return potential_map

        atomic_numbers = np.asarray(xtal.get_atomic_numbers(), dtype=np.float32)
        positions = xtal.get_positions()[:, :2]
        mask = (
            (positions[:, 0] >= x_min)
            & (positions[:, 0] < x_max)
            & (positions[:, 1] >= y_min)
            & (positions[:, 1] < y_max)
        )
        positions = positions[mask]
        atomic_numbers = atomic_numbers[mask]

        half = atom_frame // 2
        for pos, atomic_number in zip(positions, atomic_numbers):
            x_rel = (pos[0] - x_min) / pixel_size
            y_rel = (pos[1] - y_min) / pixel_size
            x_round = int(np.round(x_rel))
            y_round = int(np.round(y_rel))
            dx = x_rel - x_round
            dy = y_rel - y_round

            atom_patch = self._sub_pix_gaussian(size=atom_frame, sigma=sigma, dx=dx, dy=dy) * atomic_number
            x0 = x_round + padding - half
            y0 = y_round + padding - half
            x1 = x0 + atom_frame
            y1 = y0 + atom_frame
            if x0 < 0 or y0 < 0 or x1 > padded.shape[0] or y1 > padded.shape[1]:
                continue
            padded[x0:x1, y0:y1] += atom_patch

        potential = padded[padding:-padding, padding:-padding]
        max_val = float(np.max(potential)) if potential.size else 0.0
        if max_val > 0:
            potential = potential / max_val
        return potential.astype(np.float32)

    @staticmethod
    def _poisson_noise(image: np.ndarray, counts: float, rng: np.random.Generator) -> np.ndarray:
        image = image - image.min()
        total = float(image.sum())
        if total <= 0 or counts <= 0:
            return np.zeros_like(image, dtype=np.float32)
        image = image / total
        noisy = rng.poisson(image * counts).astype(np.float32)
        noisy -= noisy.min()
        m = float(noisy.max())
        if m > 0:
            noisy /= m
        return noisy

    @staticmethod
    def _lowfreq_noise(
        image: np.ndarray,
        noise_level: float,
        freq_scale: float,
        rng: np.random.Generator,
    ) -> np.ndarray:
        size_x, size_y = image.shape
        noise = rng.normal(0, noise_level, (size_x, size_y))
        noise_fft = np.fft.fft2(noise)
        x_freqs = np.fft.fftfreq(size_x)
        y_freqs = np.fft.fftfreq(size_y)
        freq_filter = np.outer(
            np.exp(-np.square(x_freqs) / (2 * freq_scale**2)),
            np.exp(-np.square(y_freqs) / (2 * freq_scale**2)),
        )
        filtered_noise = np.fft.ifft2(noise_fft * freq_filter).real
        filtered_noise -= filtered_noise.min()
        m = float(filtered_noise.max())
        if m > 0:
            filtered_noise /= m
        return filtered_noise.astype(np.float32)

    def _generate_sample(self, seed: int) -> None:
        rng = np.random.default_rng(int(seed))
        fov_ang = self._fov * 1e10
        sample_xy = max(fov_ang * float(self.sample_extent_scale), fov_ang * 1.2)
        sample_z = max(fov_ang * 0.6, 60.0)

        particle_radius = 16.0
        radius_std = 2.0
        aspect_ratio = 0.4
        min_separation = 3.0
        n_particles = max(1, int(self.sample_particle_count))
        max_attempts = 500

        bulk_types = {
            "Au": bulk("Au", "fcc", a=4.08),
            "Pt": bulk("Pt", "fcc", a=3.92),
            "Fe": bulk("Fe", "bcc", a=2.87),
        }
        bulk_names = list(bulk_types.keys())
        desired_angles = [(0, 0, 0), (60, 0, 0), (45, 45, 45)]

        placed_centers: list[tuple[float, float, float]] = []
        placed_particles: list[tuple[str, np.ndarray, float, tuple[float, float, float]]] = []
        particle_records: list[dict] = []

        x_min, x_max = -sample_xy * 0.5, sample_xy * 0.5
        y_min, y_max = -sample_xy * 0.5, sample_xy * 0.5
        z_mid = 0.0
        for _ in range(max_attempts * n_particles):
            if len(placed_particles) >= n_particles:
                break
            radius = float(np.clip(rng.normal(particle_radius, radius_std), 3.0, None))
            margin = radius + 2.0
            cx = float(rng.uniform(x_min + margin, x_max - margin))
            cy = float(rng.uniform(y_min + margin, y_max - margin))
            cz = z_mid

            too_close = False
            for px, py, pr in placed_centers:
                if np.hypot(cx - px, cy - py) < (radius + pr + min_separation):
                    too_close = True
                    break
            if too_close:
                continue

            placed_centers.append((cx, cy, radius))
            btype = str(rng.choice(bulk_names))
            idx = len(placed_particles)
            angles = desired_angles[idx] if idx < len(desired_angles) else tuple(rng.uniform(0, 360, size=3))
            center = np.array([cx, cy, cz], dtype=np.float64)
            placed_particles.append((btype, center, radius, angles))

            counts_dict: dict[str, int] = {}
            for symbol in bulk_types[btype].get_chemical_symbols():
                counts_dict[symbol] = counts_dict.get(symbol, 0) + 1
            total = sum(counts_dict.values())
            composition = {symbol: count / total for symbol, count in counts_dict.items()}
            particle_records.append(
                {
                    "center": center,
                    "radius": radius,
                    "btype": btype,
                    "composition": composition,
                }
            )

        all_positions: list[np.ndarray] = []
        all_symbols: list[str] = []
        for btype, center, radius, angles in placed_particles:
            this_bulk = bulk_types[btype]
            a_lat = this_bulk.cell.lengths()[0]
            z_radius = radius * aspect_ratio

            rep = int(radius * 2 / a_lat) + 3
            supercell = this_bulk.repeat((rep, rep, rep))
            positions = supercell.get_positions().copy()
            positions -= positions.mean(axis=0)
            rot = self._rotation_matrix_zyx(*angles)
            positions = positions @ rot.T
            r_scaled = np.sqrt(
                (positions[:, 0] / radius) ** 2
                + (positions[:, 1] / radius) ** 2
                + (positions[:, 2] / z_radius) ** 2
            )
            mask = r_scaled <= 1.0
            selected_positions = positions[mask] + center
            selected_symbols = [s for s, m in zip(supercell.get_chemical_symbols(), mask) if m]
            if len(selected_positions) == 0:
                continue
            all_positions.append(selected_positions)
            all_symbols.extend(selected_symbols)

        if all_positions:
            stacked_positions = np.vstack(all_positions)
            self._sample_atoms_base = Atoms(symbols=all_symbols, positions=stacked_positions)
        else:
            self._sample_atoms_base = Atoms()

        self._particle_records_base = particle_records
        self._all_sample_elements = sorted({el for rec in particle_records for el in rec["composition"]})
        self._world_bounds_ang = {
            "x_min": x_min,
            "x_max": x_max,
            "y_min": y_min,
            "y_max": y_max,
            "z_min": -sample_z * 0.5,
            "z_max": sample_z * 0.5,
        }
        self._cached_pose_key = None
        self._update_view_cache(force=True)

    def read_manufacturer(self) -> str:
        return self._manufacturer

    def read_beam_pos(self):
        return [self._beam_pos_x, self._beam_pos_y]

    def write_beam_pos(self, value):
        x, y = value[0], value[1]
        if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
            raise ValueError(f"beam_pos values must be in [0.0, 1.0], got x={x}, y={y}")
        self._beam_pos_x = float(x)
        self._beam_pos_y = float(y)

    def _acquire_stem_image(self, imsize: int, dwell_time: float, detector_list: list) -> np.ndarray:
        self._sync_stage_from_proxy()
        self._imsize = imsize
        self._update_view_cache(force=False)

        size = imsize
        fov_ang = self._fov * 1e10
        edge_crop = 20
        beam_current = 1000.0  # pA
        blur_noise_level = 0.1
        pixel_size = fov_ang / size

        frame_half = fov_ang * 0.5
        edge_ang = edge_crop * pixel_size
        frame = (-frame_half - edge_ang, frame_half + edge_ang, -frame_half - edge_ang, frame_half + edge_ang)
        potential = self._create_pseudo_potential(
            self._sample_atoms_view,
            pixel_size=pixel_size,
            sigma=1.0,
            bounds=frame,
            atom_frame=11,
        )

        ab = pt.get_target_aberrations("Spectra300", 200000)
        ab["acceleration_voltage"] = 200e3
        ab["FOV"] = fov_ang / 10.0  # nm
        ab["convergence_angle"] = 30
        ab["wavelength"] = it.get_wavelength(ab["acceleration_voltage"])

        probe, _a_k, _chi = pt.get_probe(ab, size + 2 * edge_crop, size + 2 * edge_crop, verbose=False)
        psf_shifted = np.fft.ifftshift(probe)
        image = np.fft.ifft2(np.fft.fft2(potential) * np.fft.fft2(psf_shifted))
        image = np.abs(image)
        image = image[edge_crop:-edge_crop, edge_crop:-edge_crop]

        scan_time = dwell_time * size * size
        counts = scan_time * (beam_current * 1e-12) / (1.602e-19)
        pose_seed = int(abs(hash((tuple(np.round(self._stage_position, 10)), round(self._fov, 14), size))) % (2**32))
        rng = np.random.default_rng(pose_seed + self._sample_seed_runtime)
        noisy_image = self._poisson_noise(image, counts=counts, rng=rng)
        noisy_image += self._lowfreq_noise(noisy_image, noise_level=0.1, freq_scale=0.1, rng=rng) * blur_noise_level
        return np.clip(noisy_image, 0.0, 1.0).astype(np.float32)

    def _acquire_stem_image_advanced(
        self,
        detector_names: list[str],
        base_resolution: int,
        scan_region,
        dwell_time: float,
        auto_beam_blank: bool,
    ) -> list[np.ndarray]:
        im = self._acquire_stem_image(int(base_resolution), float(dwell_time), detector_names)
        return [im.copy() for _ in detector_names]

    def _acquire_spectrum(self, detector_name: str, exposure_time: float):
        self._sync_stage_from_proxy()
        self._update_view_cache(force=False)

        px, py = self.read_beam_pos()
        fov_ang = self._fov * 1e10
        beam_x = (px - 0.5) * fov_ang
        beam_y = (py - 0.5) * fov_ang

        weighted: dict[str, float] = {}
        weight_sum = 0.0
        for rec in self._particle_records_view:
            cx, cy = rec["center"][:2]
            radius = rec["radius"]
            dist = float(np.hypot(beam_x - cx, beam_y - cy))
            if dist <= radius:
                w = max(1e-6, 1.0 - (dist / radius))
                weight_sum += w
                for element, frac in rec["composition"].items():
                    weighted[element] = weighted.get(element, 0.0) + w * frac

        spectrum_seed = int(
            abs(
                hash(
                    (
                        tuple(np.round(self._stage_position, 10)),
                        round(px, 6),
                        round(py, 6),
                        round(exposure_time, 6),
                        self._sample_seed_runtime,
                    )
                )
            )
            % (2**32)
        )
        rng = np.random.default_rng(spectrum_seed)

        if weight_sum <= 0.0:
            return {
                element: float(np.abs(rng.normal(0.0, 0.02)))
                for element in (self._all_sample_elements or ["Au", "Pt", "Fe"])
            }

        normalized = {el: val / weight_sum for el, val in weighted.items()}
        noisy = {}
        for element, value in normalized.items():
            noisy[element] = float(max(0.0, value + rng.normal(0.0, 0.01)))
        total = sum(noisy.values())
        if total <= 0.0:
            return noisy
        return {el: val / total for el, val in noisy.items()}

    def _place_beam(self, position) -> None:
        x, y = position
        self.write_beam_pos([x, y])

    def _set_fov(self, fov) -> None:
        self._fov = float(fov)

    def _get_stage(self):
        self._sync_stage_from_proxy()
        return self._stage_position

    def _move_stage(self, position):
        if len(position) != 5:
            raise ValueError("Stage position must have 5 values: [x, y, z, alpha, beta]")
        target = np.array(position, dtype=np.float64)

        std = float(self.stage_move_noise_std)
        if std > 0:
            noise = np.zeros(5, dtype=np.float64)
            noise[:3] = np.random.normal(0.0, std, size=3)
            target = target + noise

        stage = self._detector_proxies.get("stage")
        if stage is not None:
            stage.x = float(target[0])
            stage.y = float(target[1])
            stage.z = float(target[2])
            stage.alpha = float(target[3])
            stage.beta = float(target[4])

        self._stage_position = target
        self._update_view_cache(force=False)

    @command(dtype_out=str)
    def get_viewport_metadata(self) -> str:
        self._sync_stage_from_proxy()
        fov_ang = self._fov * 1e10
        stage_xyz_ang = self._stage_position[:3] * 1e10
        viewport = {
            "x_min": float(stage_xyz_ang[0] - fov_ang * 0.5),
            "x_max": float(stage_xyz_ang[0] + fov_ang * 0.5),
            "y_min": float(stage_xyz_ang[1] - fov_ang * 0.5),
            "y_max": float(stage_xyz_ang[1] + fov_ang * 0.5),
            "z_center": float(stage_xyz_ang[2]),
        }
        metadata = {
            "stage_position": [float(v) for v in self._stage_position],
            "fov_m": float(self._fov),
            "fov_angstrom": float(fov_ang),
            "imsize": int(self._imsize),
            "sample_seed": int(self._sample_seed_runtime),
            "viewport_world_angstrom": viewport,
            "world_bounds_angstrom": self._world_bounds_ang,
            "particle_count": len(self._particle_records_base),
        }
        return json.dumps(metadata)

    @command(dtype_in=int, dtype_out=str)
    def regenerate_sample(self, seed: int) -> str:
        self._sample_seed_runtime = int(seed)
        self._generate_sample(seed=self._sample_seed_runtime)
        return json.dumps(
            {
                "status": "ok",
                "sample_seed": self._sample_seed_runtime,
                "particle_count": len(self._particle_records_base),
            }
        )


if __name__ == "__main__":
    ThermoDigitalTwin.run_server()
