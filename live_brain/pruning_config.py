"""Table pruning configuration."""

PRUNING_POLICY = {
    'audit_log': {
        'enabled': True,
        'retention_days': 90,
        'batch_size': 1000
    },
    'context_impressions': {
        'enabled': True,
        'retention_days': 30,
        'batch_size': 1000
    },
    'reality_events': {
        'enabled': True,
        'retention_days': 30,
        'batch_size': 1000
    }
}
