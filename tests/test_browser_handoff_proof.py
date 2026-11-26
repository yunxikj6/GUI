import os
import unittest
from unittest.mock import AsyncMock, Mock, patch

from cloud_render import browser_handoff_proof as proof
from cloud_render.chromium_capture import CaptureError
from cloud_render.wire import Frame, Layer, encode_message


class PrivateComparison(unittest.IsolatedAsyncioTestCase):
    async def test_only_the_independently_observed_reference_can_leave_after_equality(self):
        public = Frame(1, 60, 40, (Layer(0, 0, 60, 40, b'skiapict' + bytes(25)),))
        changed = Frame(1, 60, 40, (Layer(0, 0, 60, 40, b'skiapict' + bytes(24) + b'X'),))
        for source, should_match in [(public, True), (changed, False)]:
            with self.subTest(should_match=should_match):
                emit = Mock()
                with (patch.object(proof.socket, 'if_nameindex', return_value=[(1, 'lo')]),
                      patch.object(os.path, 'samefile', return_value=True),
                      patch.object(proof, 'capture_offline_viewport', AsyncMock(return_value=(None, public))),
                      patch.object(proof, '_capture_owned_viewport', AsyncMock(return_value=(None, source)))):
                    result = await proof.compare_offline_handoff(
                        Mock(), Mock(), browser_pid=2, reference_pid=3, executable='/bin/chromium',
                        on_verified_reference=emit,
                    )
                self.assertEqual(result, proof.OfflinePaintComparison(
                    should_match, len(encode_message(public)), len(encode_message(source))))
                if should_match:
                    emit.assert_called_once_with(encode_message(public))
                else:
                    emit.assert_not_called()

    async def test_private_protocol_errors_are_redacted_and_never_emit_reference(self):
        public = Frame(1, 60, 40, (Layer(0, 0, 60, 40, b'skiapict' + bytes(25)),))
        emit = Mock()
        with (patch.object(proof.socket, 'if_nameindex', return_value=[(1, 'lo')]),
              patch.object(os.path, 'samefile', return_value=True),
              patch.object(proof, 'capture_offline_viewport', AsyncMock(return_value=(None, public))),
              patch.object(proof, '_capture_owned_viewport', AsyncMock(side_effect=RuntimeError('private-fixture')))):
            with self.assertRaises(CaptureError) as err:
                await proof.compare_offline_handoff(Mock(), Mock(), browser_pid=2, reference_pid=3,
                                                    executable='/bin/chromium', on_verified_reference=emit)
        self.assertNotIn('fixture', str(err.exception))
        emit.assert_not_called()

    def test_frame_identity_metadata_drops_all_url_and_unrelated_frame_fields(self):
        event = {'name': 'TracingStartedInBrowser', 'pid': 5, 'args': {'data': {'frames': [
            {'frame': 'own', 'processId': 24, 'url': 'fixture-a'}, {'frame': 'other', 'url': 'fixture-b'}]}}}
        self.assertEqual(proof._binding_event(event, 'own'), {
            'name': 'TracingStartedInBrowser', 'args': {'data': {'frames': [{'frame': 'own', 'processId': 24}]}}})
        self.assertFalse(proof._is_offline_handoff('https://auth.openai.com.example/oauth/authorize?fixture'))
        self.assertFalse(proof._is_offline_handoff('https://auth.openai.com/oauth/authorize?fixture#fragment'))
        self.assertFalse(proof._is_offline_handoff('https://auth.openai.com/oauth/authorize?'))
        self.assertTrue(proof._is_offline_handoff('https://auth.openai.com/oauth/authorize?fixture'))
