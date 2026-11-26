import unittest

from experiments.mutter_app.pointer_diagnostics import observed_clicks


class NativePointerDiagnosticsTest(unittest.TestCase):
    placement = 'PLACEMENT 1 84 33 16 127 67 1090 760 127 67 1090 760 127.000 67.000 1090.000 760.000'
    enter = 'POINTER_ENTER 1 84 28 33 4 140032 111360'
    down = 'POINTER_BUTTON 1 84 28 8 345 272 1'
    up = 'POINTER_BUTTON 1 84 28 9 355 272 0'

    def parse(self, events, *, commits=(16,)):
        return observed_clicks(events, client=1, pid=84, surface=33, verified_commits=set(commits))

    def test_actual_native_delivery_keeps_separate_pointer_identity_and_fixed_point_positions(self):
        result = self.parse(
            [
                self.placement,
                self.enter,
                self.down,
                'POINTER_MOTION 9 84 28 349 0 0',
                'POINTER_LEAVE 1 84 99 33 5',
                'POINTER_MOTION 1 84 28 351 140160 111424',
                self.up,
            ]
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['downOnDesktop'], [674, 502])
        self.assertEqual(result[0]['upOnDesktop'], [674.5, 502.25])

    def test_no_click_pair_after_losing_any_part_of_the_original_route(self):
        for invalidation in [
            'POINTER_LEAVE 1 84 28 33 5',
            'POINTER_RELEASE 1 84 28',
            'POINTER_SURFACE_GONE 1 84 33',
            'POINTER_DISCONNECT 1',
            self.placement.replace('127.000', '130.000'),
            self.placement.replace('33 16 ', '33 17 '),
            'POINTER_ENTER 1 84 28 44 5 140032 111360',
        ]:
            with self.subTest(invalidation=invalidation):
                self.assertEqual(
                    self.parse([self.placement, self.enter, self.down, invalidation, self.up]), []
                )
        self.assertEqual(self.parse([self.placement, self.enter, self.down, self.up], commits=()), [])
        self.assertEqual(self.parse([self.placement, self.down, self.up]), [])
