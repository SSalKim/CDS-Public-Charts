"""The sparse circular search must preserve the original dense search exactly."""
import unittest
from unittest.mock import patch

import numpy as np
from scipy.ndimage import binary_erosion, find_objects, label, maximum_filter, minimum_filter

from public_charts.plotter import extrema_candidates, smooth_valid,center_candidates,merge_similar_centers
from public_charts.chart_style import center_parameters, numeric_extrema_parameters


def dense_reference(value, latitude, longitude, radius_km, prominence, high, valid_mask=None):
    lat_step = float(np.median(np.abs(np.diff(latitude))))
    lon_step = float(np.median(np.abs(np.diff(longitude))))
    ry = max(2, int(radius_km / (111 * lat_step)))
    rx = max(2, int(radius_km / (111 * .8 * lon_step)))
    fy, fx = np.ogrid[-ry:ry+1, -rx:rx+1]
    footprint = (fy / ry)**2 + (fx / rx)**2 <= 1
    if valid_mask is not None and not np.any(valid_mask):
        return []
    smooth = smooth_valid(np.asarray(value, dtype=float), .8, valid_mask)
    search = smooth if valid_mask is None else np.where(valid_mask, smooth, -np.inf if high else np.inf)
    neighborhood = (maximum_filter if high else minimum_filter)(search, footprint=footprint, mode="nearest")
    background = smooth_valid(smooth, (max(2, ry/2), max(2, rx/2)), valid_mask)
    strength = (smooth - background) * (1 if high else -1)
    tolerance = 64*np.finfo(float).eps*max(1, float(np.nanmax(np.abs(value))))
    peaks = (smooth == neighborhood) & (strength > max(prominence, tolerance)) & np.isfinite(value)
    if valid_mask is not None:
        peaks &= binary_erosion(valid_mask, iterations=3)
    peaks[:2] = peaks[-2:] = False
    peaks[:, :2] = peaks[:, -2:] = False
    components, _ = label(peaks)
    result = []
    for number, region in enumerate(find_objects(components), 1):
        if region is None:
            continue
        cells = np.argwhere(components[region] == number)
        row, col = cells[np.argmax(strength[region][components[region] == number])]
        row, col = row + region[0].start, col + region[1].start
        local = value[row-1:row+2, col-1:col+2]
        if valid_mask is not None:
            local = np.where(valid_mask[row-1:row+2, col-1:col+2], local, np.nan)
        index = np.nanargmax(local) if high else np.nanargmin(local)
        dy, dx = np.unravel_index(index, local.shape)
        row, col = row - 1 + dy, col - 1 + dx
        result.append((float(strength[row, col]), row, col, float(value[row, col])))
    return sorted(result, reverse=True)


class ExtremaTests(unittest.TestCase):
    def check_field(self, value, spacing, radius, mask=None, prominence=0):
        latitude = 20 + np.arange(value.shape[0]) * spacing
        longitude = 90 + np.arange(value.shape[1]) * spacing
        for high in (False, True):
            with self.subTest(spacing=spacing, radius=radius, high=high, masked=mask is not None):
                expected = dense_reference(value, latitude, longitude, radius, prominence, high, mask)
                self.assertEqual(extrema_candidates(value, latitude, longitude, radius, prominence, high, mask), expected)

    def test_native_grid_scales_and_random_fields(self):
        value = np.random.default_rng(20261117).normal(size=(59, 73))
        for spacing in (.125, .15, .25):
            for radius in (20, 125, 400):
                self.check_field(value, spacing, radius, prominence=.05)

    def test_masked_holes_and_borders(self):
        y, x = np.mgrid[:65, :79]
        value = np.sin(x/5) * np.cos(y/7) + np.random.default_rng(3).normal(0, .1, x.shape)
        mask = (y > 1) & (x > 4) & ~((x > 27) & (x < 39) & (y > 13) & (y < 51))
        for radius in (60, 250, 1000):
            self.check_field(value, .15, radius, mask)
        self.check_field(value, .15, 250, np.zeros_like(mask))

    def test_flat_tied_and_monotonic_fields(self):
        y, x = np.mgrid[:35, :43]
        for value in (np.zeros(x.shape), x.astype(float), ((x//9) % 2).astype(float),
                      np.maximum(0, 1 - np.maximum(abs(x - 20), abs(y - 16))/5)):
            self.check_field(value, .125, 200)

    def test_higher_corner_outside_circle_does_not_suppress_center(self):
        value = np.zeros((61, 65))
        value[30, 32] = 8
        value[15, 13] = 12
        value[44, 51] = -10
        self.check_field(value, .125, 250)

    def test_full_grid_filter_is_separable_not_a_dense_ellipse(self):
        value = np.random.default_rng(4).normal(size=(51, 61))
        latitude, longitude = np.arange(51)*.125, np.arange(61)*.125
        with patch("public_charts.plotter.maximum_filter", wraps=maximum_filter) as filtered:
            extrema_candidates(value, latitude, longitude, 250, 0, True)
        self.assertEqual(filtered.call_count, 1)
        self.assertIn("size", filtered.call_args.kwargs)
        self.assertNotIn("footprint", filtered.call_args.kwargs)

    def test_selected_dense_models_are_more_selective_and_other_models_keep_their_settings(self):
        for domain in ("asia", "hkor"):
            for model in ("cmc", "icon"):
                with self.subTest(domain=domain, model=model):
                    old = center_parameters(domain, "temperature")
                    new = center_parameters(domain, "temperature", model)
                    self.assertGreater(new[0], old[0])
                    self.assertGreater(new[1], old[1])
                    for kind in ("height", "pressure"):
                        selected=center_parameters(domain, kind, model)
                        if model=="cmc":
                            self.assertEqual(selected,center_parameters(domain,kind))
                        else:
                            self.assertGreater(selected[0],center_parameters(domain,kind)[0])
                            self.assertGreater(selected[1],center_parameters(domain,kind)[1])
                    old = numeric_extrema_parameters(domain, "vorticity")
                    new = numeric_extrema_parameters(domain, "vorticity", model)
                    for key in ("radius", "prominence", "minimum"):
                        self.assertGreater(new[key], old[key])
                    for kind in ("precipitation", "wind"):
                        selected=numeric_extrema_parameters(domain,kind,model)
                        if model=="cmc":
                            self.assertEqual(selected,numeric_extrema_parameters(domain,kind))
                        else:
                            self.assertGreater(selected["radius"],numeric_extrema_parameters(domain,kind)["radius"])
                            self.assertGreater(selected["prominence"],numeric_extrema_parameters(domain,kind)["prominence"])
            for model in ("aifs", "ecmwf", "aigfs"):
                self.assertEqual(center_parameters(domain, "temperature", model), center_parameters(domain, "temperature"))
                self.assertEqual(numeric_extrema_parameters(domain, "vorticity", model), numeric_extrema_parameters(domain, "vorticity"))

    def test_ukmo_requires_stronger_and_wider_centers_without_affecting_other_models(self):
        for domain in ("asia","hkor"):
            for kind in ("pressure","height","temperature"):
                old=center_parameters(domain,kind)
                new=center_parameters(domain,kind,"ukmo")
                self.assertGreater(new[0],old[0]);self.assertGreater(new[1],old[1])
                for model in ("aifs","gfs","ecmwf","aigfs"):
                    if model!="gfs":
                        self.assertEqual(center_parameters(domain,kind,model),old)
            for kind in ("precipitation","vorticity","wind"):
                old=numeric_extrema_parameters(domain,kind)
                new=numeric_extrema_parameters(domain,kind,"ukmo")
                self.assertGreater(new["prominence"],old["prominence"])
                self.assertGreater(new["radius"],old["radius"])

    def test_ukmo_drops_weak_temperature_ripples_but_retains_a_distinct_core(self):
        latitude=np.linspace(29,45,161);longitude=np.linspace(115,138,231)
        x,y=np.meshgrid(longitude,latitude)
        field=.2*np.exp(-((x-121)**2+(y-34)**2)/.4)+3*np.exp(-((x-131)**2+(y-40)**2)/.6)
        old=center_candidates(field,latitude,longitude,"hkor","temperature",True)
        new=center_candidates(field,latitude,longitude,"hkor","temperature",True,model_id="ukmo")
        self.assertGreater(len(old),len(new))
        self.assertEqual(len(new),1)
        self.assertGreater(new[0][3],2.5)

    def test_gfs_korean_pressure_drops_weak_ripples_but_keeps_distinct_high_and_low(self):
        latitude=np.linspace(29,45,161);longitude=np.linspace(115,138,231)
        x,y=np.meshgrid(longitude,latitude)
        field=1020+.15*np.sin(x*5)*np.cos(y*5)
        field+=5*np.exp(-((x-121)**2+(y-40)**2)/.8)
        field-=5*np.exp(-((x-132)**2+(y-34)**2)/.8)
        old=[];new=[]
        for high in (True,False):
            old.extend(center_candidates(field,latitude,longitude,"hkor","pressure",high))
            new.extend(center_candidates(field,latitude,longitude,"hkor","pressure",high,model_id="gfs"))
        self.assertGreater(len(old),len(new)*5)
        self.assertEqual(len(new),2)
        self.assertGreater(max(item[3] for item in new),1024)
        self.assertLess(min(item[3] for item in new),1016)
        self.assertEqual(center_parameters("hkor","pressure","gfs"),(.7,180))
        self.assertGreater(center_parameters("asia","pressure","gfs")[0],center_parameters("asia","pressure")[0])

    def test_gfs_and_icon_raise_all_marker_criteria_in_both_domains(self):
        for domain in ("asia","hkor"):
            for model in ("gfs","icon"):
                for kind in ("pressure","height","temperature"):
                    old=center_parameters(domain,kind)
                    new=center_parameters(domain,kind,model)
                    self.assertGreater(new[0],old[0])
                    self.assertGreater(new[1],old[1])
                for kind in ("precipitation","wind","vorticity"):
                    old=numeric_extrema_parameters(domain,kind)
                    new=numeric_extrema_parameters(domain,kind,model)
                    self.assertGreater(new["radius"],old["radius"])
                    self.assertGreater(new["prominence"],old["prominence"])
                    self.assertGreaterEqual(new["minimum"],old["minimum"])

    def test_nearby_similar_pressure_centers_merge_but_a_deep_low_is_preserved(self):
        latitude=np.array([35.,35.5,36.]);longitude=np.array([125.,125.5,126.])
        candidates=[(2.,1,1,1023.,"H","blue"),(1.9,2,2,1022.,"L","red"),
                    (1.8,0,0,1015.,"L","red")]
        kept=merge_similar_centers(candidates,latitude,longitude,250,2)
        self.assertEqual([item[3] for item in kept],[1023.,1015.])


if __name__ == "__main__":
    unittest.main()
