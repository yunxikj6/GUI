import gzip
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from cloud_render.viz_picture import VizError, VizPending, compose


VIEW = [0, 0, 8, 8]
IDENTITY = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]


def state(index=0, **changes):
    return dict(index=index, sorting_context_id=0, blend_mode='SrcOver', opacity=1,
                is_fast_rounded_corner=False, quad_to_target_transform=IDENTITY,
                mask_filter_info='MaskFilterInfo{0.000,0.000 0.000x0.000, rectangular}',
                quad_layer_rect=VIEW) | changes


def quad(material=5, index=0, **changes):
    return dict(material=material, shared_quad_state={'index': index}, rect=VIEW,
                visible_rect=VIEW, color='rgba(1, 0, 0, 1') | changes


def render_pass(name, quads, states):
    return dict(id='CompositorRenderPass/' + name, output_rect=VIEW, quad_list=quads,
                shared_quad_state_list=states, transform_to_root_target=IDENTITY)


def scene(*passes):
    return {'frame': {'render_passes': passes}, 'active_tiles': [], 'active_tree': {'layers': []}}


class VizCommandsTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), '需要 Node 验证发布的 CanvasKit 解码器')
    def test_local_decoder_replays_viz_order_alpha_clip_and_nested_mask(self):
        mask = quad(index=1, color='rgba(0, 0, 0, 0.5')
        child = render_pass('child', [mask, quad()], [state(), state(1, blend_mode='DstIn')])
        nested = quad(material=3, render_pass_id={'id_ref': 'child'})
        base = quad(index=1, color='rgba(0, 0, 1, 1')
        root = render_pass('root', [nested, base],
                           [state(clip_rect=[0, 0, 4, 8], opacity=.5), state(1)])
        raw = compose(scene(child, root), 8, 8, None)
        vendor = Path(__file__).resolve().parents[1] / 'thin_client' / 'vendor'
        js = """
          const fs = require('node:fs');
          const vendor = process.argv[1];
          (async () => {
            const kit = await require(vendor + '/canvaskit.js')({
              locateFile: () => process.argv[2]
            });
            const skp = kit.MakePicture(fs.readFileSync(0));
            const surface = kit.MakeSurface(8, 8);
            surface.getCanvas().drawPicture(skp);
            const snapshot = surface.makeImageSnapshot();
            const info = {width: 1, height: 1, alphaType: kit.AlphaType.Unpremul,
                          colorType: kit.ColorType.RGBA_8888, colorSpace: kit.ColorSpace.SRGB};
            console.log(JSON.stringify([2, 6].map(x => Array.from(snapshot.readPixels(x, 4, info)))));
          })().catch(err => { console.error(err); process.exitCode = 1; });
        """
        with tempfile.TemporaryDirectory() as scratch:
            wasm = Path(scratch) / 'canvaskit.wasm'
            wasm.write_bytes(gzip.decompress((vendor / 'canvaskit.wasm.gz').read_bytes()))
            result = subprocess.run(['node', '-e', js, str(vendor), str(wasm)], input=raw,
                                    capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        a, b = json.loads(result.stdout)
        self.assertTrue(63 <= a[0] <= 65 and 190 <= a[2] <= 192)
        self.assertEqual((a[1], a[3]), (0, 255))
        self.assertEqual(b, [0, 0, 255, 255])

    def test_tiles_need_one_matching_active_layer_and_raster_origin(self):
        tile = quad(material=10, resource_id=3, tex_coord_rect=VIEW)
        model = scene(render_pass('root', [tile], [state()]))
        layer = dict(layer_id=5, draws_content=True, layer_quad=[0, 0, 8, 0, 8, 8, 0, 8],
                     pictures=[{'id_ref': 'picture'}])
        model['active_tree']['layers'].append(layer)
        active = dict(layer_id=5, has_resource=True, resolution='HIGH_RESOLUTION', content_rect=VIEW,
                      raster_transform={'scale': [1, 1], 'translation': [0, 0]})
        model['active_tiles'].append(active)
        refs = []
        def source(ref):
            refs.append(ref)
            return 0, 0, b'skiapict-test'
        self.assertIn(b'skiapict-test', compose(model, 8, 8, source))
        self.assertEqual(refs, [(5, 'picture')])
        tile['tex_coord_rect'] = [1, 0, 8, 8]
        with self.assertRaises(VizPending):
            compose(model, 8, 8, source)
        tile['tex_coord_rect'] = VIEW
        model['active_tree']['layers'].append(dict(layer, layer_id=7))
        model['active_tiles'].append(dict(active, layer_id=7))
        with self.assertRaises(VizError):
            compose(model, 8, 8, source)

    def test_unknown_material_or_filter_and_cyclic_pass_are_rejected(self):
        models = [scene(render_pass('root', [quad(material=4)], [state()])),
                  scene(render_pass('root', [quad()], [state(blend_mode='Multiply')])),
                  scene(render_pass('root', [quad(material=3, render_pass_id={'id_ref': 'root'})],
                                    [state()]))]
        for model in models:
            with self.subTest(model=model), self.assertRaises(VizError):
                compose(model, 8, 8, None)
