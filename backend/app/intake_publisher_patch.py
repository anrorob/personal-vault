"""Prepare a reviewed legacy root publisher update without changing its executor.

Offline transformer: input is an exact-hash verified installed source; output is
reviewed and installed by the deployment package, never executed here.
"""
import ast
import hashlib

ADAPTER = '''
from arrival_publisher_loop import drain_queues
from arrival_publication_coordination import publication_lock, cancellation

_bulk_original_process_request = process_request

def process_request(request_path):
    with publication_lock(QUEUE, timeout=None):
        if not request_path.exists():
            return
        if request_path.is_symlink() or request_path.parent != QUEUE:
            raise ValueError("Unsafe publication request path")
        document = json.loads(request_path.read_text())
        request = document.get("request", {})
        signature = document.get("signature", "")
        key = KEY.read_bytes()
        if not isinstance(request, dict) or not isinstance(signature, str) or not hmac.compare_digest(hmac.new(key, canonical(request), hashlib.sha256).hexdigest(), signature):
            raise ValueError("Invalid signed publication request")
        if cancellation(QUEUE, request["item_id"], key) is not None:
            os.replace(request_path, request_path.with_suffix(".cancelled.request"))
            return
        return _bulk_original_process_request(request_path)

def _bulk_reject(request_path, reason):
    with publication_lock(QUEUE, timeout=None):
        if request_path.exists():
            reject(request_path, reason)

def main():
    if os.geteuid() != 0:
        raise SystemExit("Root publisher required")
    if sys.argv[1:] != ["--drain"]:
        return _single_pass_main()
    supplier_queue, _ = supplier_roots()
    failures = drain_queues(((QUEUE, process_request), (supplier_queue, process_supplier_remediation)), _bulk_reject)
    if failures:
        raise SystemExit(f"{failures} requests isolated for recovery")

if __name__ == "__main__":
    main()
'''


def prepare_patch(source: str, expected_sha256: str) -> str:
    if hashlib.sha256(source.encode()).hexdigest() != expected_sha256:
        raise ValueError('Installed publisher changed; re-audit before patching')
    tree = ast.parse(source)
    main = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'main']
    guards = [n for n in tree.body if isinstance(n, ast.If) and '__name__' in ast.unparse(n.test)]
    required = {'process_request','process_supplier_remediation','supplier_roots','reject','canonical'}
    names = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    if len(main) != 1 or len(guards) != 1 or not required.issubset(names) or '_bulk_original_process_request' in source:
        raise ValueError('Unsupported publisher; no patch prepared')
    lines = source.splitlines(keepends=True)
    # The entrypoint guard must be the final statement, without hidden suffixes.
    if tree.body[-1] is not guards[0]:
        raise ValueError('Unexpected publisher entrypoint')
    lines = lines[:guards[0].lineno-1]
    lines[main[0].lineno-1] = lines[main[0].lineno-1].replace('def main(', 'def _single_pass_main(', 1)
    output = ''.join(lines) + ADAPTER
    ast.parse(output)
    return output