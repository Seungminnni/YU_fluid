import unittest
from dataclasses import asdict
from cfd_twin.wall_diagnostics import polygon_key, join_rows, summary
from cfd_twin.production_mesh import MESH_PRESETS


class WallDiagnosticsTests(unittest.TestCase):
    def test_polygon_mapping_survives_order_but_rejects_changed_surface(self):
        vertices=[(0.,0.,0.),(1.,0.,0.),(0.,1.,0.)]
        key=polygon_key(vertices)
        self.assertEqual(key,polygon_key(vertices[::-1]))
        layer=[dict(battery='C01',face_key=key,area_m2=.5,nSurfaceLayers=0)]
        flow=[dict(battery='C01',face_key=polygon_key(vertices[1:]+vertices[:1]),area_m2=.5,yPlus=2.)]
        self.assertEqual(join_rows(layer,flow)[0]['nSurfaceLayers'],0)
        with self.assertRaises(ValueError):join_rows(layer,flow+flow)
        with self.assertRaises(ValueError):join_rows(layer,[])
        with self.assertRaises(ValueError):join_rows(layer,[dict(flow[0],battery='C02')])
        with self.assertRaises(ValueError):join_rows(layer,[dict(flow[0],area_m2=.6)])

    def test_overlap_is_area_weighted_and_threshold_is_strict(self):
        result=summary([
            dict(area_m2=1,nSurfaceLayers=0,yPlus=2),
            dict(area_m2=3,nSurfaceLayers=3,yPlus=1),
            dict(area_m2=6,nSurfaceLayers=10,yPlus=.5),
        ])
        self.assertAlmostEqual(result['area_fraction_yplus_below_1'],.6)
        self.assertAlmostEqual(result['fraction_of_high_yplus_area_without_layers'],.25)
        self.assertAlmostEqual(result['area_fraction_by_layer_count']['0'],.1)

    def test_ab_presets_change_only_shrinker_and_have_authorized_cell_range(self):
        a=asdict(MESH_PRESETS['production_frame_fit_8um_10layers_ab_A'])
        b=asdict(MESH_PRESETS['production_frame_fit_8um_10layers_ab_B'])
        self.assertEqual({k for k in a if a[k]!=b[k]},{'mesh_shrinker'})
        self.assertEqual(a['minimum_total_cells'],600000)
        self.assertAlmostEqual(a['target_cells']*(1+a['total_tolerance_fraction']),690000)
        self.assertEqual(a['minimum_hexahedron_fraction'],.88)
