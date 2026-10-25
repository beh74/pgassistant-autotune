"""Reviewed executable recommendations. Unknown/new advisors require a catalog update.

Derived from pgAssistant advisor_enriched.yml and Executive Plan normalization.
Only the primary collector source authorizes a recommendation after deduplication.
"""

PROFILE_ACTIONS = {
    "global_advisor": {"ALTER_TABLE", "CREATE_INDEX", "DROP_INDEX", "ANALYZE", "VACUUM", "CONFIG_CHANGE", "REVIEW_ONLY", "OTHER"},
    "index_advisor": {"CREATE_INDEX"},
    "autovacuum": {"ANALYZE", "VACUUM", "CONFIG_CHANGE"},
    "parameter_advisor": {"CONFIG_CHANGE"},
}

# (source, advisor_id, collector action_type); unsupported types are never expanded.
EXECUTABLE_RECOMMENDATIONS = (
    ('global_advisor', 'missing_useful_foreign_key_indexes', 'CREATE_INDEX'),
    ('global_advisor', 'redundant_non_unique_indexes_covered_by_unique', 'DROP_INDEX'),
    ('global_advisor', 'strictly_duplicate_unused_indexes', 'DROP_INDEX'),
    ('global_advisor', 'partially_duplicate_low_usage_indexes', 'DROP_INDEX'),
    ('global_advisor', 'unused_non_constraint_indexes', 'DROP_INDEX'),
    ('global_advisor', 'Tables with potentially stale statistics', 'ANALYZE'),
    ('global_advisor', 'Tables with estimated bloat', 'VACUUM'),
    ('global_advisor', 'Invalid or unusable indexes', 'DROP_INDEX'),
    ('global_advisor', 'Tables never vacuumed', 'VACUUM'),
    ('global_advisor', 'autovacuum_dead_tuple_vacuum_urgency', 'VACUUM'),
    ('index_advisor', 'index_opportunity', 'CREATE_INDEX'),
    ('index_advisor', 'order_by_index', 'CREATE_INDEX'),
    ('index_advisor', 'order_by_limit_index', 'CREATE_INDEX'),
    ('index_advisor', 'group_by_index', 'CREATE_INDEX'),
    ('autovacuum', 'never_vacuumed', 'ANALYZE'),
    ('autovacuum', 'never_vacuumed', 'VACUUM'),
    ('autovacuum', 'never_analyzed', 'ANALYZE'),
    ('autovacuum', 'stale_vacuum', 'VACUUM'),
    ('autovacuum', 'stale_analyze', 'VACUUM'),
    ('autovacuum', 'table_autovacuum', 'ANALYZE'),
    ('autovacuum', 'table_autovacuum', 'VACUUM'),
    ('autovacuum', 'autovacuum', 'CONFIG_CHANGE'),
    ('autovacuum', 'autovacuum_naptime', 'CONFIG_CHANGE'),
    ('autovacuum', 'autovacuum_vacuum_scale_factor', 'CONFIG_CHANGE'),
    ('autovacuum', 'autovacuum_vacuum_threshold', 'CONFIG_CHANGE'),
    ('autovacuum', 'autovacuum_analyze_scale_factor', 'CONFIG_CHANGE'),
    ('autovacuum', 'autovacuum_analyze_threshold', 'CONFIG_CHANGE'),
    ('autovacuum', 'autovacuum_vacuum_cost_delay', 'CONFIG_CHANGE'),
    ('autovacuum', 'autovacuum_vacuum_cost_limit', 'CONFIG_CHANGE'),
    ('autovacuum', 'log_autovacuum_min_duration', 'CONFIG_CHANGE'),
)

# Include non-executable recommendations so exclusions can describe the full catalog.
PROFILE_ADVISORS = {
    source: {advisor for src, advisor, _ in EXECUTABLE_RECOMMENDATIONS if src == source}
    for source in PROFILE_ACTIONS
}
PROFILE_ADVISORS["global_advisor"].update({
    "foreign_key_columns_different_data_types", "Tables with significant buffer cache misses",
    "Important PostgreSQL settings disabled or suboptimal", "Tables without primary key",
    "High index-to-table size ratio", "Low or missing foreign key coverage",
    "table_autovacuum_maintenance_flagged", "table_autovacuum_cluster_load",
    "long_running_transactions", "Sequences approaching maximum value", "PostgreSQL release upgrade",
})
PROFILE_ADVISORS["autovacuum"].add("autovacuum_max_workers")
PROFILE_ADVISORS["parameter_advisor"].update({
    "pgtune", "work_mem", "effective_cache_size", "random_page_cost",
    "effective_io_concurrency", "max_parallel_workers_per_gather", "max_wal_size",
    "PostgreSQL parameters",
})
