"""Current source boundary: no alternative model/Lab implementation can drift back."""
from pathlib import Path
import ast
import re

ROOT = Path(__file__).parents[2]


def test_current_runtime_has_no_retired_import_or_module():
    for directory in ('backend/app','video_analyser','src'):
        for path in (ROOT/directory).rglob('*'):
            if not path.is_file() or path.suffix not in ('.py','.tsx','.ts'):
                continue
            assert not re.search(r'video_lab|penguin|glm_|minicpm|internvl', path.name, re.I), path
            text=path.read_text(encoding='utf-8')
            if path.suffix=='.py':
                for node in ast.walk(ast.parse(text)):
                    if isinstance(node,ast.ImportFrom):
                        assert not re.search(r'video_lab|penguin|glm_|minicpm|internvl',node.module or '',re.I), path
