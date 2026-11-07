"""Minimal run-scoped D-108 Asset Identity registry; no classification."""

KNOWN_TYPES = frozenset({'WEB_PAGE', 'CONTENT_BLOCK', 'IMAGE', 'VIDEO', 'AUDIO', 'DOCUMENT',
                         'NAVIGATION_STRUCTURE', 'FORM', 'SEARCH', 'FILTER', 'CALCULATOR', 'INTERACTIVE_MAP'})
EXCLUDED_SOURCES = frozenset({'TECHNICAL_RESOURCE', 'EXTERNAL_DEPENDENCY'})


class AssetIdentityRegistry:
    def __init__(self, ids):
        self.ids = ids
        self.assets = []
        self._identities = {}

    def register(self, *, normalized_final_url, asset_type, asset_category,
                 confirmed_final_url, source_type, observation_status, schema_version, primary_url=None):
        if source_type in EXCLUDED_SOURCES:
            raise ValueError('D-094 source cannot have a standalone Asset')
        if not asset_category or not observation_status or not schema_version:
            raise ValueError('asset metadata required')
        eligible = (confirmed_final_url is True and isinstance(normalized_final_url, str)
                    and bool(normalized_final_url) and asset_type in KNOWN_TYPES)
        key = (self.ids.run_id, normalized_final_url, asset_type) if eligible else None
        if key is not None and key in self._identities:
            return self._identities[key], False
        asset = {'schema_version': schema_version, 'run_id': self.ids.run_id,
                 'asset_id': self.ids.next('AST'), 'asset_category': asset_category,
                 'asset_type': asset_type, 'observation_status': observation_status,
                 'primary_url': primary_url, 'evidence_ids_json': '[]'}
        self.assets.append(asset)
        if key is not None:
            self._identities[key] = asset
        return asset, True
