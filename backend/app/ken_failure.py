"""Bounded failure classification over existing KEN run evidence."""


def failure_info(run):
    config = run.get('configuration') or {}
    diagnostic = config.get('failure_diagnostic') or {}
    error = run.get('error') or ''
    code, message, retryable = 'requires_attention', 'KEN could not finish. Technical review is required.', False
    if error == 'Interrupted by worker restart':
        code, message, retryable = 'worker_interrupted', 'Analysis was interrupted by a worker restart.', True
    elif error in ('Analyser unavailable or inference timeout', 'Inference timeout'):
        code, message, retryable = 'service_timeout', 'The analyser was unavailable or timed out.', True
    elif diagnostic.get('service', {}).get('http_status') in (409, 429, 502, 503, 504):
        code, message, retryable = 'service_unavailable', 'The analyser was temporarily unavailable.', True
    elif diagnostic.get('stage') == 'result_persistence' and diagnostic.get('exception_type') in (
            'OperationalError', 'ConnectionTimeout', 'SerializationFailure', 'DeadlockDetected'):
        code, message, retryable = 'persistence_interrupted', 'Saving the analysis was temporarily interrupted.', True
    elif diagnostic.get('code') == 'ken_duration_limit':
        code, message = 'ken_duration_limit', 'This video exceeds the supported KEN analysis duration.'
    elif error in ('Published Home Video is unavailable', 'Published Home Video is unavailable for the owner'):
        code, message = 'source_unavailable', 'The canonical video is unavailable for analysis.'
    elif diagnostic.get('stage') == 'input_preparation':
        code, message = 'input_preparation_failed', 'Video input preparation failed. Technical review is required.'
    elif diagnostic.get('stage') in ('input_integrity', 'result_integrity'):
        code, message = 'integrity_failed', 'Analysis integrity verification failed. Technical review is required.'
    elif error == 'Model returned no description':
        code, message = 'empty_response', 'The model returned no usable description. Technical review is required.'
    return {'code': code, 'message': message, 'retryable': retryable,
            'retry_allowed': retryable and not config.get('recovery_retry_of'),
            'failed_at': run.get('completed_at')}
