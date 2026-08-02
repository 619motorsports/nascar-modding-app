import ast
import collections
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ACTIVE_BACKEND_FILES = (
    ROOT / 'app.py',
    ROOT / 'containers.py',
    *sorted(
        path
        for package in ('core', 'editing', 'formats', 'games', 'verification')
        for path in (ROOT / 'nascar_modding' / package).glob('*.py')
    ),
)


def _top_level_functions(tree):
    return [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


class BackendStructureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sources = {
            path: path.read_text(encoding='utf-8')
            for path in ACTIVE_BACKEND_FILES
        }
        cls.trees = {
            path: ast.parse(source, filename=str(path))
            for path, source in cls.sources.items()
        }

    def test_active_backend_has_no_duplicate_function_names(self):
        definitions = collections.defaultdict(list)
        for path, tree in self.trees.items():
            for node in _top_level_functions(tree):
                definitions[node.name].append(f'{path.name}:{node.lineno}')

        duplicates = {
            name: locations
            for name, locations in definitions.items()
            if len(locations) > 1
        }
        self.assertEqual({}, duplicates)

    def test_active_backend_has_no_identical_function_bodies(self):
        bodies = collections.defaultdict(list)
        for path, tree in self.trees.items():
            for node in _top_level_functions(tree):
                body = ast.Module(body=node.body, type_ignores=[])
                fingerprint = ast.dump(body, include_attributes=False)
                bodies[fingerprint].append(f'{node.name} ({path.name}:{node.lineno})')

        duplicates = [locations for locations in bodies.values() if len(locations) > 1]
        self.assertEqual([], duplicates)

    def test_dynamic_module_import_logic_is_centralized(self):
        calls = []
        for path, tree in self.trees.items():
            for function in _top_level_functions(tree):
                for node in ast.walk(function):
                    if (
                        isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr == 'spec_from_file_location'
                    ):
                        calls.append(f'{path.name}:{function.name}')
        self.assertEqual(['modules.py:load_module'], calls)

    def test_hd_writer_modes_delegate_to_one_implementation(self):
        functions = {
            node.name: node
            for node in _top_level_functions(self.trees[ROOT / 'app.py'])
        }
        expected = {
            '_native_hd_patch_wrapper': '_shared_livery_wrapper_editor',
            '_native_hd_patch_wrapper_public_v1': '_shared_livery_wrapper_editor',
        }
        for name, owner in expected.items():
            node = functions[name]
            self.assertFalse(any(isinstance(child, ast.For) for child in ast.walk(node)))
            calls = {
                child.func.id
                for child in ast.walk(node)
                if isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
            }
            self.assertIn(owner, calls)

    def test_editor_json_state_uses_shared_writer(self):
        functions = {
            node.name: node
            for node in _top_level_functions(self.trees[ROOT / 'app.py'])
        }
        state_savers = (
            'save_cfg',
            '_save_selector_cfg',
            '_ui_save_mapping_overrides',
            '_rp_save_history',
            '_team_state_save',
        )
        for name in state_savers:
            calls = {
                child.func.id
                for child in ast.walk(functions[name])
                if isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
            }
            self.assertIn('atomic_write_json', calls, name)

    def test_managed_uid_diagnostics_are_owned_by_shared_service(self):
        app_tree = self.trees[ROOT / 'app.py']
        names = {node.name for node in _top_level_functions(app_tree)}
        self.assertNotIn('_extra_uid_store', names)
        self.assertNotIn('_extra_apply_uid_pool', names)
        self.assertNotIn('_extra_reconcile_state_with_live_database', names)
        functions = {node.name: node for node in _top_level_functions(app_tree)}
        for route, method in (('extra_uid_pool', 'uid_pool'),
                              ('extra_uid_verdict', 'record_uid_verdict')):
            calls = {
                node.func.attr for node in ast.walk(functions[route])
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            }
            self.assertIn(method, calls)

        paint_calls = {
            node.func.attr for node in ast.walk(functions['paint_system_check_api'])
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        self.assertIn('paint_system_check', paint_calls)

    def test_game_specific_asset_and_guard_logic_is_centralized(self):
        app_source = self.sources[ROOT / 'app.py']
        self.assertNotIn('DLC_LIV_', app_source)
        self.assertNotIn('_n14_', app_source)
        self.assertNotIn("ACTIVE_GAME=='nascar14'", app_source)
        self.assertIn('classify_livery_slot(ACTIVE_GAME, n)', app_source)

    def test_backup_path_policy_comes_from_shared_editing_service(self):
        app_tree = self.trees[ROOT / 'app.py']
        self.assertNotIn('backup_path', {node.name for node in _top_level_functions(app_tree)})
        self.assertIn('from nascar_modding.editing.archive import (', self.sources[ROOT / 'app.py'])

    def test_durable_writers_come_from_shared_core(self):
        names = {node.name for node in _top_level_functions(self.trees[ROOT / 'app.py'])}
        self.assertNotIn('atomic_write_bytes', names)
        self.assertNotIn('atomic_write_json', names)
        self.assertIn('from nascar_modding.core.files import atomic_write_bytes, atomic_write_json', self.sources[ROOT / 'app.py'])

    def test_legacy_frontend_delegates_shared_driver_editors(self):
        functions = {
            node.name: node
            for node in _top_level_functions(self.trees[ROOT / 'app.py'])
        }

        def called_attributes(function_name):
            return {
                node.func.attr
                for node in ast.walk(functions[function_name])
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            }

        self.assertIn('ratings', called_attributes('read_stats'))
        self.assertIn('set_rating', called_attributes('write_stat'))
        self.assertIn('restore', called_attributes('reset_stats'))
        self.assertIn('rename', called_attributes('_apply_display_name'))
        api_calls = {
            node.func.id
            for node in ast.walk(functions['api_name'])
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        self.assertIn('_apply_display_name', api_calls)
        handle_calls = called_attributes('api_handle')
        self.assertIn('rename_current', handle_calls)


if __name__ == '__main__':
    unittest.main()
