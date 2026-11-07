"""D-095 run-scoped sequential identifiers for the authorized foundation."""


class RunIds:
    def __init__(self, run_id):
        if not isinstance(run_id, str) or not run_id:
            raise ValueError('run_id required')
        self.run_id = run_id
        self._counts = {'SRC': 0, 'AST': 0, 'EVD': 0, 'REL': 0, 'OCC': 0}

    def next(self, prefix):
        if prefix not in self._counts:
            raise ValueError('unsupported identifier type')
        self._counts[prefix] += 1
        return f'{prefix}-{self._counts[prefix]:06d}'
