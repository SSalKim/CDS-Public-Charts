"""Requested labels and shared scalar fields for lower-air shading/contours."""
import unittest
from unittest.mock import patch

import numpy as np
from cartopy.mpl.geoaxes import GeoAxes

from public_charts import plotter
from public_charts.chart_style import PUBLIC_PROFILE, contour_steps


class ContourTests(unittest.TestCase):
    def test_label_reduction_is_limited_to_the_requested_asia_products(self):
        self.assertEqual(contour_steps("asia",500,PUBLIC_PROFILE)["temperature_label"],6)
        for level in (700,850,925):
            steps=contour_steps("asia",level,PUBLIC_PROFILE)
            self.assertEqual((steps["theta_anchor"],steps["theta_label"]),(333,12))
        for level in (200,300,500,700,850,925):
            steps=contour_steps("hkor",level,PUBLIC_PROFILE)
            self.assertNotIn("temperature_label",steps)
            self.assertNotIn("theta_label",steps)
            self.assertNotIn("temperature_label",contour_steps("asia",level))

    def render(self,product,domain):
        latitude,longitude=np.linspace(0,80,121),np.linspace(35,180,181)
        x,y=np.meshgrid(longitude,latitude)
        level=int(product[-3:])
        theta=333+.8*(y-38)+1.7*np.sin(x*2)*np.cos(y*3)
        arrays={"latitude":latitude,"longitude":longitude,
                f"geopotential_{level}":(5500+9*y)*9.80665,
                f"temperature_{level}":273.15-30+.5*y,
                f"wind_u_{level}":8+0*x,f"wind_v_{level}":5+0*y}
        lines,fills=[],[]
        original_line,original_fill=GeoAxes.contour,GeoAxes.contourf
        def contour(axes,*args,**kwargs):
            result=original_line(axes,*args,**kwargs)
            lines.append((result,np.ma.array(args[2],copy=True)))
            return result
        def contourf(axes,*args,**kwargs):
            result=original_fill(axes,*args,**kwargs)
            fills.append((result,np.ma.array(args[2],copy=True)))
            return result
        with patch.object(GeoAxes,"add_feature"), \
             patch.object(plotter,"korean_admin_boundaries",return_value=[]), \
             patch.object(plotter,"equivalent_potential_temperature",return_value=theta), \
             patch.object(GeoAxes,"contour",new=contour), \
             patch.object(GeoAxes,"contourf",new=contourf):
            plotter.render(arrays,"2026111706",24,product,domain,None,
                           profile=PUBLIC_PROFILE,return_frame=True,field_masks={level:np.ones(x.shape,dtype=bool)})
        return lines,fills

    def test_asia_temperature_lines_remain_three_degrees_but_labels_are_six(self):
        lines,_=self.render("gph500","asia")
        line=next(obj for obj,_ in lines if obj.get_gid()=="temperature-contours")
        np.testing.assert_equal(np.diff(line.levels),3)
        self.assertTrue(line.labelLevelList)
        self.assertTrue(all(level%6==0 for level in line.labelLevelList))

    def test_theta_lines_and_fill_share_exact_values_and_masks(self):
        for domain in ("asia","hkor"):
            with self.subTest(domain=domain):
                lines,fills=self.render("ept850",domain)
                line,values=next(item for item in lines if item[0].get_gid()=="theta-e-contours")
                _,shading=next(item for item in fills if item[0].get_gid()=="shaded-field")
                np.testing.assert_equal(values.data,shading.data)
                np.testing.assert_equal(np.ma.getmaskarray(values),np.ma.getmaskarray(shading))
                self.assertTrue(line.labelLevelList)
                if domain=="asia":
                    self.assertTrue(all((level-333)%12==0 for level in line.labelLevelList))
                    self.assertIn(333,line.labelLevelList)
                    np.testing.assert_equal(np.diff(line.levels),6)
                else:
                    np.testing.assert_equal(line.labelLevelList,line.levels)
                    np.testing.assert_equal(np.diff(line.levels),3)


if __name__=="__main__":
    unittest.main()
