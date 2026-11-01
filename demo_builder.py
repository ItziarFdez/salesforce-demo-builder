#!/usr/bin/env python3
"""Compile a reviewed demo specification into a Salesforce DX project.

No network calls during build. apply creates a NEW scratch org; it never accepts
an existing target org or commands supplied by a model.
"""
import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

NS = 'http://soap.sforce.com/2006/04/metadata'
ET.register_namespace('', NS)
TYPES = {'Text', 'Email', 'Phone', 'Url', 'Number', 'Currency', 'Date', 'Checkbox', 'Picklist'}
API_NAME = re.compile(r'^[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)*__c$')


class DemoError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise DemoError(message)


def text(value, name, maximum=80):
    require(isinstance(value, str) and bool(value.strip()) and len(value) <= maximum,
            name + ' must be a nonempty string of at most ' + str(maximum) + ' characters')
    require(all(ord(c) >= 32 for c in value), name + ' contains control characters')


def validate(spec):
    require(isinstance(spec, dict), 'Specification must be an object')
    require(set(spec) <= {'name', 'business_problem', 'api_version', 'objects', 'demo_steps'},
            'Unknown top-level specification keys')
    text(spec.get('name'), 'name')
    text(spec.get('business_problem'), 'business_problem', 2000)
    require(re.fullmatch(r'\d{2,3}\.0', spec.get('api_version', '66.0')) is not None,
            'api_version must look like 66.0')
    objects = spec.get('objects')
    require(isinstance(objects, list) and 1 <= len(objects) <= 10, 'Provide 1–10 custom objects')
    seen = set()
    for obj in objects:
        require(isinstance(obj, dict) and set(obj) <= {'api_name', 'label', 'plural_label', 'fields'},
                'Object has unsupported keys')
        name = obj.get('api_name', '')
        require(isinstance(name, str) and len(name) <= 40 and API_NAME.fullmatch(name) is not None,
                'Invalid custom object API name')
        require(name.casefold() not in seen, 'Duplicate object API name')
        seen.add(name.casefold())
        text(obj.get('label'), 'Object label', 40)
        text(obj.get('plural_label'), 'Object plural label', 40)
        fields = obj.get('fields', [])
        require(isinstance(fields, list) and len(fields) <= 30, 'At most 30 fields per object')
        field_names = set()
        for field in fields:
            require(isinstance(field, dict) and set(field) <= {'api_name', 'label', 'type', 'length', 'values'},
                    'Field has unsupported keys')
            name = field.get('api_name', '')
            require(isinstance(name, str) and len(name) <= 40 and API_NAME.fullmatch(name) is not None, 'Invalid field API name')
            require(name.casefold() not in field_names, 'Duplicate field API name')
            field_names.add(name.casefold())
            text(field.get('label'), 'Field label', 40)
            kind = field.get('type')
            require(isinstance(kind, str) and kind in TYPES, 'Unsupported field type')
            if kind == 'Text':
                length = field.get('length', 255)
                require(type(length) is int and 1 <= length <= 255, 'Text length must be 1–255')
            else:
                require('length' not in field, 'length is only supported for Text fields')
            if kind == 'Picklist':
                values = field.get('values')
                require(isinstance(values, list) and 1 <= len(values) <= 50, 'Picklist requires 1–50 values')
                for value in values:
                    text(value, 'Picklist value', 40)
                require(len(set(values)) == len(values), 'Duplicate picklist values')
            else:
                require('values' not in field, 'values is only supported for Picklist fields')
    steps = spec.get('demo_steps', [])
    require(isinstance(steps, list) and len(steps) <= 20, 'Provide at most 20 demo steps')
    for step in steps:
        text(step, 'Demo step', 1000)
    return spec


def node(tag, value=None, parent=None):
    element = ET.Element('{' + NS + '}' + tag) if parent is None else ET.SubElement(parent, '{' + NS + '}' + tag)
    if value is not None:
        element.text = str(value)
    return element


def xml(root):
    return ET.tostring(root, encoding='utf-8', xml_declaration=True).decode() + '\n'


def field_xml(field):
    root = node('CustomField')
    node('fullName', field['api_name'], root)
    if field['type'] == 'Checkbox':
        node('defaultValue', 'false', root)
    node('label', field['label'], root)
    if field['type'] == 'Text':
        node('length', field.get('length', 255), root)
    if field['type'] in {'Number', 'Currency'}:
        node('precision', 18, root)
        node('scale', 2, root)
    node('type', field['type'], root)
    if field['type'] == 'Picklist':
        value_set = node('valueSet', parent=root)
        node('restricted', 'true', value_set)
        definition = node('valueSetDefinition', parent=value_set)
        node('sorted', 'false', definition)
        for label in field['values']:
            value = node('value', parent=definition)
            node('fullName', label, value)
            node('default', 'false', value)
            node('label', label, value)
    return xml(root)


def artifacts(spec):
    """Only known metadata is generated; arbitrary Apex/Flow/model code is unsupported."""
    base = 'force-app/main/default/'
    result = {}
    permission = node('PermissionSet')
    visibility = node('applicationVisibilities', parent=permission)
    node('application', 'Demo_Workspace', visibility)
    node('visible', 'true', visibility)
    node('description', 'Access to objects generated by Salesforce Demo Builder', permission)
    for obj in spec['objects']:
        for field in obj.get('fields', []):
            access = node('fieldPermissions', parent=permission)
            node('editable', 'true', access)
            node('field', obj['api_name'] + '.' + field['api_name'], access)
            node('readable', 'true', access)
    node('label', 'Demo Builder Access', permission)
    profile = node('Profile')
    for obj in spec['objects']:
        api = obj['api_name']
        root = node('CustomObject')
        node('deploymentStatus', 'Deployed', root)
        node('label', obj['label'], root)
        name = node('nameField', parent=root)
        node('label', obj['label'][:30] + ' Name', name)
        node('type', 'Text', name)
        node('pluralLabel', obj['plural_label'], root)
        node('sharingModel', 'ReadWrite', root)
        result[base + 'objects/' + api + '/' + api + '.object-meta.xml'] = xml(root)
        for field in obj.get('fields', []):
            result[base + 'objects/' + api + '/fields/' + field['api_name'] + '.field-meta.xml'] = field_xml(field)
        tab = node('CustomTab')
        node('customObject', 'true', tab)
        node('motif', 'Custom18: Form', tab)
        result[base + 'tabs/' + api + '.tab-meta.xml'] = xml(tab)
        layout_name = 'Demo Layout'
        layout = node('Layout')
        section = node('layoutSections', parent=layout)
        node('customLabel', 'false', section)
        node('detailHeading', 'true', section)
        node('editHeading', 'true', section)
        node('label', 'Information', section)
        column = node('layoutColumns', parent=section)
        item = node('layoutItems', parent=column)
        node('behavior', 'Required', item)
        node('field', 'Name', item)
        for field in obj.get('fields', []):
            item = node('layoutItems', parent=column)
            node('behavior', 'Edit', item)
            node('field', field['api_name'], item)
        node('style', 'OneColumn', section)
        result[base + 'layouts/' + api + '-' + layout_name + '.layout-meta.xml'] = xml(layout)
        assignment = node('layoutAssignments', parent=profile)
        node('layout', api + '-' + layout_name, assignment)
        access = node('objectPermissions', parent=permission)
        for tag, value in [('allowCreate', 'true'), ('allowDelete', 'false'), ('allowEdit', 'true'),
                           ('allowRead', 'true'), ('modifyAllRecords', 'false'), ('object', api),
                           ('viewAllRecords', 'false')]:
            node(tag, value, access)
    for obj in spec['objects']:
        tab = node('tabSettings', parent=permission)
        node('tab', obj['api_name'], tab)
        node('visibility', 'Visible', tab)
    result[base + 'permissionsets/DemoBuilderAccess.permissionset-meta.xml'] = xml(permission)
    result[base + 'profiles/Admin.profile-meta.xml'] = xml(profile)
    app = node('CustomApplication')
    node('formFactors', 'Large', app)
    node('isNavAutoTempTabsDisabled', 'false', app)
    node('label', spec['name'], app)
    node('navType', 'Standard', app)
    for obj in spec['objects']:
        node('tabs', obj['api_name'], app)
    node('uiType', 'Lightning', app)
    result[base + 'applications/Demo_Workspace.app-meta.xml'] = xml(app)
    result['sfdx-project.json'] = json.dumps({'packageDirectories': [{'path': 'force-app', 'default': True}],
        'namespace': '', 'sourceApiVersion': spec.get('api_version', '66.0')}, indent=2) + '\n'
    result['config/project-scratch-def.json'] = json.dumps({'orgName': spec['name'], 'edition': 'Developer',
        'settings': {'lightningExperienceSettings': {'enableS1DesktopEnabled': True}}}, indent=2) + '\n'
    result['demo-spec.json'] = json.dumps(spec, indent=2, ensure_ascii=False) + '\n'
    steps = spec.get('demo_steps', [])
    result['DEMO-RUNBOOK.md'] = '# ' + spec['name'] + '\n\n' + spec['business_problem'] + '\n\n' + \
        '\n'.join(str(i + 1) + '. ' + step for i, step in enumerate(steps)) + \
        '\n\n## Validate in the scratch org\n\n- Open Demo Workspace in the App Launcher.\n' + \
        '- Create and edit a record in each generated object.\n- Check every picklist and numeric field.\n' + \
        '- Record observed results and unmet requirements before presenting.\n' + \
        '\nNo sample records, Flows, Apex, or AI agents are generated by this compiler.\n'
    return result


def digest_files(files):
    payload = json.dumps(files, sort_keys=True, separators=(',', ':')).encode()
    return hashlib.sha256(payload).hexdigest()


def build(spec_path, output):
    spec = validate(json.loads(Path(spec_path).read_text()))
    output = Path(output)
    require(not output.exists(), 'Output already exists; use a new output directory')
    files = artifacts(spec)
    manifest = {path: hashlib.sha256(content.encode()).hexdigest() for path, content in files.items()}
    output.mkdir(parents=True)
    try:
        for path, content in files.items():
            target = output / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding='utf-8')
        (output / '.demo-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    except BaseException:
        shutil.rmtree(output)
        raise
    return digest_files(manifest)


def verified_manifest(output):
    root = Path(output)
    require(root.is_dir() and not root.is_symlink(), 'Invalid project directory')
    manifest_path = root / '.demo-manifest.json'
    require(not manifest_path.is_symlink(), 'Manifest cannot be a symlink')
    manifest = json.loads(manifest_path.read_text())
    require(isinstance(manifest, dict) and bool(manifest), 'Invalid manifest')
    for relative, expected in manifest.items():
        path = root / relative
        require(not Path(relative).is_absolute() and '..' not in Path(relative).parts, 'Unsafe manifest path')
        require(root.resolve() in path.resolve().parents and not path.is_symlink(), 'Unsafe project path')
        require(path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == expected,
                'Generated files changed; rebuild and review a new plan')
    allowed = set(manifest) | {'.demo-manifest.json', '.execution.json'}
    for path in root.rglob('*'):
        require(not path.is_symlink(), 'Symlinks are not allowed')
        if path.is_file():
            require(path.relative_to(root).as_posix() in allowed, 'Unexpected file in generated project')
    # The manifest is not a signature: independently recompile and compare, so
    # modifying metadata AND its hash cannot inject unreviewed components.
    spec = validate(json.loads((root / 'demo-spec.json').read_text()))
    canonical = {p: hashlib.sha256(c.encode()).hexdigest() for p, c in artifacts(spec).items()}
    require(manifest == canonical, 'Manifest does not match the supported compiler output')
    return manifest


def sf_json(arguments, cwd):
    executable = shutil.which('sf')
    require(executable is not None, 'Install Salesforce CLI first')
    try:
        process = subprocess.run([executable] + arguments + ['--json'], cwd=cwd,
                                 capture_output=True, text=True, timeout=1200)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DemoError('Salesforce CLI did not finish; check the scratch org in your Dev Hub before retrying') from exc
    try:
        response = json.loads(process.stdout)
    except ValueError as exc:
        raise DemoError('Salesforce CLI returned an unreadable response; raw output was not saved') from exc
    require(process.returncode == 0 and response.get('status') == 0,
            'Salesforce CLI step failed; raw output was not saved because it can contain credentials')
    return response.get('result')


def apply(output, approval, hub, alias, days):
    root = Path(output).resolve()
    manifest = verified_manifest(root)
    require(approval == digest_files(manifest), 'Approval hash does not match the reviewed build')
    require(re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,39}', alias) is not None, 'Use a simple scratch org alias')
    require(isinstance(hub, str) and bool(hub) and not hub.startswith('-'), 'Specify an authorized personal Dev Hub')
    require(1 <= days <= 30, 'Scratch org lifetime must be 1–30 days')
    receipt = root / '.execution.json'
    require(not receipt.exists(), 'This build already has an execution attempt; inspect it before starting another build')
    # Read-only preflight. Never overwrite an existing alias or use a default target.
    aliases = sf_json(['alias', 'list'], root)
    require(isinstance(aliases, list), 'Unexpected Salesforce CLI alias response')
    require(not any(a.get('alias') == alias for a in aliases), 'Alias already exists; choose a new one')
    state = {'started_at': datetime.now(timezone.utc).isoformat(), 'steps': [], 'status': 'started'}

    def save():
        receipt.write_text(json.dumps(state, indent=2) + '\n')

    save()
    try:
        state['current_step'] = 'create'
        save()
        created = sf_json(['org', 'create', 'scratch', '--definition-file', 'config/project-scratch-def.json',
                           '--target-dev-hub', hub, '--alias', alias, '--duration-days', str(days), '--wait', '10'], root)
        require(isinstance(created, dict), 'Unexpected scratch org creation response')
        username = created.get('username')
        require(isinstance(username, str) and bool(username) and not username.startswith('-'),
                'New scratch org username missing; stopping without deploying')
        state['scratch_username'] = username
        state['steps'].append('create')
        state['current_step'] = 'deploy'
        save()
        sf_json(['project', 'deploy', 'start', '--source-dir', 'force-app', '--target-org', username, '--wait', '10'], root)
        state['steps'].append('deploy')
        state['current_step'] = 'permissions'
        save()
        sf_json(['org', 'assign', 'permset', '--name', 'DemoBuilderAccess', '--target-org', username], root)
        state['steps'].append('permissions')
        state['status'] = 'complete'
    except BaseException:
        state['status'] = 'incomplete'
        save()
        raise
    save()
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    b = sub.add_parser('build', help='Validate a specification and generate metadata locally')
    b.add_argument('spec')
    b.add_argument('--out', required=True)
    p = sub.add_parser('plan', help='Verify generated files and show their approval hash')
    p.add_argument('project')
    a = sub.add_parser('apply', help='Create a NEW scratch org and deploy the reviewed build')
    a.add_argument('project')
    a.add_argument('--approve', required=True)
    a.add_argument('--dev-hub', required=True)
    a.add_argument('--alias', required=True)
    a.add_argument('--days', type=int, default=1)
    args = parser.parse_args()
    try:
        if args.command == 'build':
            print(json.dumps({'status': 'built_locally', 'approval_hash': build(args.spec, args.out)}, indent=2))
        elif args.command == 'plan':
            manifest = verified_manifest(args.project)
            print(json.dumps({'files': sorted(manifest), 'approval_hash': digest_files(manifest),
                'actions': ['create new scratch org', 'deploy generated metadata', 'assign permission set'],
                'network_calls': 'Only when apply is explicitly run'}, indent=2))
        else:
            print(json.dumps(apply(args.project, args.approve, args.dev_hub, args.alias, args.days), indent=2))
        return 0
    except (DemoError, OSError, ValueError, TypeError, KeyError) as exc:
        print('Error: ' + str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
