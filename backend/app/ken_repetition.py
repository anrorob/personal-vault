"""Mechanical loop detection only. Never edits or interprets model language."""
VERSION = 'repeated-block-v1'
WARNING = 'Repeated output loop detected; generation was bounded. The model output has not been rewritten.'


def repeated_tail(sequence, minimum=32, maximum=256):
    """Three identical consecutive substantial blocks; normal repetition is left alone."""
    for width in range(minimum, min(maximum, len(sequence) // 3) + 1):
        if sequence[-width:] == sequence[-2*width:-width] == sequence[-3*width:-2*width]:
            return True
    return False


def warn_on_repetition(result):
    text = result.get('description')
    if isinstance(text, str):
        words = text.split()
        if any(repeated_tail(words[:end], minimum=48) for end in range(144, len(words) + 1)):
            result = {**result, 'warnings': [*result.get('warnings', []), WARNING],
                      'repetition_guard': VERSION}
    return result
