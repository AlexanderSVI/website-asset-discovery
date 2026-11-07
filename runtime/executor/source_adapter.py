"""Convert one Executor fetch result to one Source and transient provenance."""
from copy import deepcopy
from url_normalizer import normalize_url


class SourceAdapter:
    def __init__(self, ids, schema_version):
        if not schema_version:
            raise ValueError('schema_version required')
        self.ids = ids
        self.schema_version = schema_version

    def adapt(self, result, *, source_type, observation_status):
        if not source_type or not observation_status:
            raise ValueError('source_type and observation_status required')
        requested = result['url']
        attempts = result.get('attempts', [])
        final = None
        for attempt in attempts:
            # Worker records status only after parsing status and headers.
            if type(attempt.get('http_status')) is int and isinstance(attempt.get('url'), str):
                final = attempt['url']
        normalized = None
        normalization_error = None
        if final is not None:
            try:
                normalized = normalize_url(final)
            except (ValueError, UnicodeError) as error:
                normalization_error = type(error).__name__
        source = {'schema_version': self.schema_version, 'run_id': self.ids.run_id,
                  'source_id': self.ids.next('SRC'), 'source_type': source_type,
                  'requested_url': requested, 'final_url': final,
                  'normalized_final_url': normalized, 'observation_status': observation_status}
        # Provenance stays separate from the approved Source CSV fields.
        history = {'attempts': deepcopy(attempts), 'redirects': deepcopy(result.get('redirects', [])),
                   'reason': result.get('reason'), 'normalization_error': normalization_error}
        return source, history
