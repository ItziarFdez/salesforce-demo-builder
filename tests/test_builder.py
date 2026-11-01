import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET

import demo_builder as builder

EXAMPLE = Path(__file__).resolve().parents[1] / 'examples/service-demo.json'


class BuilderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'demo'
        self.spec = json.loads(EXAMPLE.read_text())

    def build(self):
        return builder.build(EXAMPLE, self.root)

    def test_build_generates_valid_xml_and_stable_plan_without_sf(self):
        with patch.object(builder, 'sf_json', side_effect=AssertionError('Network attempted')):
            token = self.build()
            self.assertEqual(token, builder.digest_files(builder.verified_manifest(self.root)))
            for path in self.root.rglob('*.xml'):
                ET.parse(path)
            self.assertTrue((self.root / 'force-app/main/default/applications/Demo_Workspace.app-meta.xml').exists())

    def test_path_traversal_is_rejected_before_creating_output(self):
        self.spec['objects'][0]['api_name'] = '../Account__c'
        with self.assertRaises(builder.DemoError):
            builder.validate(self.spec)
        self.assertFalse(self.root.exists())

    def test_unsupported_automation_is_not_silently_ignored(self):
        self.spec['flows'] = [{'name': 'SendEmail'}]
        with self.assertRaises(builder.DemoError):
            builder.validate(self.spec)

    def test_labels_cannot_control_output_paths(self):
        self.spec['objects'][0]['label'] = '../../outside'
        result = builder.artifacts(builder.validate(self.spec))
        self.assertTrue(all('..' not in Path(path).parts for path in result))
        self.assertIn('force-app/main/default/layouts/Service_Request__c-Demo Layout.layout-meta.xml', result)

    def test_xml_escapes_business_labels(self):
        self.spec['objects'][0]['label'] = 'Requests & Delivery'
        result = builder.artifacts(builder.validate(self.spec))
        content = result['force-app/main/default/objects/Service_Request__c/Service_Request__c.object-meta.xml']
        self.assertIn('&amp;', content)
        self.assertEqual(ET.fromstring(content).find('m:label', {'m': builder.NS}).text, 'Requests & Delivery')

    def test_metadata_tampering_fails_even_when_manifest_is_rehashed(self):
        self.build()
        manifest_path = self.root / '.demo-manifest.json'
        manifest = json.loads(manifest_path.read_text())
        target = 'force-app/main/default/objects/Service_Request__c/Service_Request__c.object-meta.xml'
        (self.root / target).write_text('<bad/>')
        manifest[target] = hashlib.sha256(b'<bad/>').hexdigest()
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaises(builder.DemoError):
            builder.verified_manifest(self.root)

    def test_extra_component_prevents_apply(self):
        token = self.build()
        (self.root / 'force-app/injected.cls').write_text('unreviewed')
        with patch.object(builder, 'sf_json') as sf, self.assertRaises(builder.DemoError):
            builder.apply(self.root, token, 'personal-hub', 'demo-one', 1)
        sf.assert_not_called()

    def test_bad_approval_never_calls_cli(self):
        self.build()
        with patch.object(builder, 'sf_json') as sf, self.assertRaises(builder.DemoError):
            builder.apply(self.root, 'wrong', 'personal-hub', 'demo-one', 1)
        sf.assert_not_called()

    def test_apply_targets_only_username_returned_by_creation(self):
        token = self.build()
        with patch.object(builder, 'sf_json', side_effect=[[], {'username': 'new@scratch.example'}, {}, {}]) as sf:
            result = builder.apply(self.root, token, 'personal-hub', 'demo-one', 1)
        self.assertEqual(result['status'], 'complete')
        calls = [call.args[0] for call in sf.call_args_list]
        self.assertIn('personal-hub', calls[1])
        self.assertIn('new@scratch.example', calls[2])
        self.assertIn('new@scratch.example', calls[3])
        self.assertNotIn('--set-default', sum(calls, []))

    def test_existing_alias_is_never_reused(self):
        token = self.build()
        with patch.object(builder, 'sf_json', return_value=[{'alias': 'demo-one'}]) as sf:
            with self.assertRaises(builder.DemoError):
                builder.apply(self.root, token, 'personal-hub', 'demo-one', 1)
        self.assertEqual(sf.call_count, 1)
        self.assertFalse((self.root / '.execution.json').exists())

    def test_failed_deployment_records_partial_progress_and_blocks_retry(self):
        token = self.build()
        with patch.object(builder, 'sf_json', side_effect=[[], {'username': 'new@scratch.example'}, builder.DemoError('failed')]):
            with self.assertRaises(builder.DemoError):
                builder.apply(self.root, token, 'personal-hub', 'demo-one', 1)
        state = json.loads((self.root / '.execution.json').read_text())
        self.assertEqual(state['status'], 'incomplete')
        self.assertEqual(state['steps'], ['create'])
        self.assertEqual(state['current_step'], 'deploy')
        with patch.object(builder, 'sf_json') as sf, self.assertRaises(builder.DemoError):
            builder.apply(self.root, token, 'personal-hub', 'demo-two', 1)
        sf.assert_not_called()


if __name__ == '__main__':
    unittest.main()
