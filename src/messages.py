# SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
# SPDX-License-Identifier: MIT

"""Fixed application and installer text, with stable, unversioned message keys.

Keep complete messages here and use named format parameters. Preserve literal
braces as {{ and }}. Status codes, protocol fields, tool descriptions and
report content belong to their existing modules. No locale is selected here.
"""

MESSAGES = {
    'cli.add_technical_event_details_to_stderr_never_print': (
        'Add technical event details to stderr, never print prompts, credentials or raw model output.'
    ),
    'cli.cannot_list_codex_models': 'Cannot list Codex models: {exc}\n',
    'cli.check_configuration_source_state_and_cli_capabilities_no': (
        'Check configuration, source state, and CLI capabilities; no branch switch or model calls.'
    ),
    'cli.codex_model_catalogs_account_access_has_not_been': (
        'Codex model catalogs. Account access has not been checked.'
    ),
    'cli.config_is_required_unless_using_tui': '--config is required unless using --tui.',
    'cli.description': (
        'Sequential architecture auditing with configured coding-agent CLIs.\n\nmacOS or Linux, Python '
        '3.11+, and one or more authenticated coding-agent CLIs.\nGit >= 2.34.1 is required only for git '
        'mode; folder mode needs no Git.\nTextual provides the interactive interface. Git branches are '
        'checked out sequentially in place.\nThe parent publishes reports. The agent receives stdin and '
        'returns structured JSON.'
    ),
    'cli.disable_the_spinner_and_periodic_waiting_messages_keep': (
        'Disable the spinner and periodic waiting messages, keep stage boundaries, warnings and errors.'
    ),
    'cli.in_command_line_mode_stdout_contains_one_result': (
        'In command-line mode, stdout contains one result. --help and argument syntax errors use standard '
        'argparse output without a run or manifest. Console messages are in English, output_language only '
        'controls generated reports. Labels and statuses are colored automatically when their output stream '
        'is a terminal, unless NO_COLOR is nonempty or TERM=dumb. JSON is never colored. Disable color with '
        'NO_COLOR=1.'
    ),
    'cli.install_the_required_tui_dependency_with_python_m': (
        'Install the required TUI dependency with: python3 -m pip install -r {value}'
    ),
    'cli.list_codex_model_catalogs_for_the_configured_active': (
        'List Codex model catalogs for the configured active agents; no access probes or audit.'
    ),
    'cli.model': 'Model',
    'cli.model_catalog_request_interrupted': 'Model catalog request interrupted.\n',
    'cli.models_cannot_be_combined_with_check_or_tui': '--models cannot be combined with --check or --tui.',
    'cli.name': 'Name',
    'cli.no_models_returned': 'No models returned.',
    'cli.open_the_interactive_config_picker_and_dashboard_also': (
        'Open the interactive config picker and run dashboard (also the default without arguments).'
    ),
    'cli.path_to_a_json_or_jsonc_configuration_required': (
        'Path to a JSON or JSONC configuration, required outside the TUI.'
    ),
    'cli.received_signal': 'Received signal {signum}',
    'cli.result_format_on_stdout_text_is_human_readable': (
        'Result format on stdout: text is human-readable, auto selects text for a TTY, JSON otherwise '
        '(default: auto). Diagnostics use stderr.'
    ),
    'cli.stages': '{executable} | Stages: {value}',
    'cli.supported_in_api': 'Supported in API',
    'cli.the_configuration_has_no_active_codex_agents': 'The configuration has no active Codex agents.',
    'cli.the_tui_needs_an_interactive_terminal_use_config': (
        'The TUI needs an interactive terminal. Use --config FILE for command-line output.'
    ),
    'cli.trust_only_the_configured_git_checkout_and_verified': (
        'Trust only the configured Git checkout and verified submodules despite an ownership mismatch (Git '
        '>= 2.34.1).'
    ),
    'cli.tui_cannot_be_combined_with_output_text_or': '--tui cannot be combined with --output text or --output json.',
    'cli.unknown': 'unknown',
    'cli.visibility': 'Visibility',

    'diagnostic.architecture_format.output_language_requires_russian_or_english_architecture_headings': (
        'output_language requires Russian or English architecture headings'
    ),

    'diagnostic.artifacts.no_temporary_directory_outside_the_source_tree_is': (
        'No temporary directory outside the source tree is available.'
    ),

    'diagnostic.attempts.model_binding_changed_during_invocation': 'Model binding changed during invocation.',
    'diagnostic.attempts.private_attempt_artifact_changed': 'Private attempt artifact changed.',

    'diagnostic.audit.check_the_configuration_syntax_and_check_again': (
        'Check the configuration syntax and run --check again.'
    ),
    'diagnostic.audit.could_not_save_last_analysis_in_the_configuration': (
        'Could not save last_analysis in the configuration: {exc}'
    ),
    'diagnostic.audit.document_processing_completed_with_citation_warnings_unresolved_citations': (
        'Document processing completed with citation warnings, unresolved citations require human review.'
    ),
    'diagnostic.audit.enabled_stage_policy_checks_satisfied_factual_correctness_is': (
        'Enabled-stage policy checks satisfied, factual correctness is not established.'
    ),
    'diagnostic.audit.local_launch_prerequisites_checked_source_analysis_was_not': (
        'Local launch prerequisites checked. Source analysis was not performed, model availability and '
        'provider authorization were not tested.'
    ),
    'diagnostic.audit.processing_may_be_incomplete_or_evidence_may_be': (
        'Processing may be incomplete or evidence may be insufficient, consult limitations and diagnostics.'
    ),
    'diagnostic.audit.the_configuration_is_not_valid_json_jsonc': 'The configuration is not valid JSON/JSONC.',
    'diagnostic.audit.this_implementation_requires_macos_or_linux_and_python': (
        'This implementation requires macOS or Linux and Python 3.11+.'
    ),

    'diagnostic.codex.codex_did_not_write_a_final_response': 'Codex did not write a final response.',
    'diagnostic.codex.codex_has_no_independent_shell_tool_control': 'Codex has no independent shell_tool control.',
    'diagnostic.codex.codex_mcp_transport_failed': 'Codex MCP transport failed.',
    'diagnostic.codex.mcp_is_unavailable_to_the_report_writer_and': (
        'MCP is unavailable to the report writer and comparison stages.'
    ),
    'diagnostic.codex.mcp_requires_a_pinned_codex_model_catalog': 'MCP requires a pinned Codex model catalog.',
    'diagnostic.codex.mcp_requires_an_absolute_private_manifest_path_and': (
        'MCP requires an absolute private manifest path and its SHA-256.'
    ),
    'diagnostic.codex.the_required_source_mcp_server_failed_to_start': (
        'The required source MCP server failed to start; inspect private attempt logs.'
    ),
    'diagnostic.codex.the_source_reading_stage_has_no_private_mcp': (
        'The source-reading stage has no private MCP manifest.'
    ),

    'diagnostic.codex_capabilities.cannot_launch_the_codex_capability_probe': (
        'Cannot launch the Codex capability probe.'
    ),
    'diagnostic.codex_capabilities.cannot_read_the_codex_capability_trace': 'Cannot read the Codex capability trace.',
    'diagnostic.codex_capabilities.codex_changed_the_model_selected_for_the_mcp': (
        'Codex changed the model selected for the MCP capability check.'
    ),
    'diagnostic.codex_capabilities.codex_changed_the_model_within_a_linked_inference': (
        'Codex changed the model within a linked inference response chain.'
    ),
    'diagnostic.codex_capabilities.codex_did_not_expose_the_complete_source_result': (
        'Codex did not expose the complete source result text.'
    ),
    'diagnostic.codex_capabilities.codex_did_not_identify_the_effective_request_model': (
        'Codex did not identify the effective request model.'
    ),
    'diagnostic.codex_capabilities.codex_did_not_produce_a_complete_request_tool': (
        'Codex did not produce a complete request tool set for one model.'
    ),
    'diagnostic.codex_capabilities.codex_did_not_produce_a_private_capability_trace': (
        'Codex did not produce a private capability trace.'
    ),
    'diagnostic.codex_capabilities.codex_exposes_a_different_tool_set_than_the': (
        'Codex exposes a different tool set than the MCP access policy permits.'
    ),
    'diagnostic.codex_capabilities.codex_exposes_an_unsupported_native_tool': (
        'Codex exposes an unsupported native tool.'
    ),
    'diagnostic.codex_capabilities.codex_omitted_the_inference_identity_needed_to_verify': (
        'Codex omitted the inference identity needed to verify its tool schemas.'
    ),
    'diagnostic.codex_capabilities.codex_omitted_tool_schemas_without_a_verified_prior': (
        'Codex omitted tool schemas without a verified prior response.'
    ),
    'diagnostic.codex_capabilities.codex_returned_an_invalid_capability_trace': (
        'Codex returned an invalid capability trace.'
    ),
    'diagnostic.codex_capabilities.codex_returned_an_invalid_capability_trace_event': (
        'Codex returned an invalid capability trace event.'
    ),
    'diagnostic.codex_capabilities.codex_returned_an_unlinked_or_ambiguous_completed_inference': (
        'Codex returned an unlinked or ambiguous completed inference response.'
    ),
    'diagnostic.codex_capabilities.codex_returned_an_unnamed_tool_namespace': (
        'Codex returned an unnamed tool namespace.'
    ),
    'diagnostic.codex_capabilities.codex_returned_an_unsafe_request_trace_path': (
        'Codex returned an unsafe request trace path.'
    ),
    'diagnostic.codex_capabilities.codex_returned_an_unsupported_request_input_list': (
        'Codex returned an unsupported request input list.'
    ),
    'diagnostic.codex_capabilities.codex_returned_an_unsupported_tool_schema': (
        'Codex returned an unsupported tool schema.'
    ),
    'diagnostic.codex_capabilities.codex_returned_an_unsupported_tool_schema_list': (
        'Codex returned an unsupported tool schema list.'
    ),
    'diagnostic.codex_capabilities.codex_returned_invalid_bootstrap_instruction_source_metadata': (
        'Codex returned invalid bootstrap instruction source metadata.'
    ),
    'diagnostic.codex_capabilities.codex_reused_an_inference_identity_in_its_request': (
        'Codex reused an inference identity in its request trace.'
    ),
    'diagnostic.codex_capabilities.codex_source_results_were_absent_or_truncated_in': (
        'Codex source results were absent or truncated in the model request.'
    ),
    'diagnostic.codex_capabilities.codex_trace_contains_a_symbolic_link': 'Codex trace contains a symbolic link.',
    'diagnostic.codex_capabilities.the_codex_mcp_capability_probe_failed': 'The Codex MCP capability probe failed.',

    'diagnostic.codex_catalog.codex_cannot_load_its_bundled_model_catalog_for': (
        'Codex cannot load its bundled model catalog for MCP.'
    ),
    'diagnostic.codex_catalog.codex_failed_to_report_its_bundled_model_catalog': (
        'Codex failed to report its bundled model catalog for MCP.'
    ),
    'diagnostic.codex_catalog.codex_must_provide_a_nonempty_bundled_model_catalog': (
        'Codex must provide a nonempty bundled model catalog for MCP.'
    ),
    'diagnostic.codex_catalog.codex_returned_invalid_bundled_model_catalog_json': (
        'Codex returned invalid bundled model catalog JSON.'
    ),
    'diagnostic.codex_catalog.codex_returned_invalid_or_duplicate_bundled_model_identities': (
        'Codex returned invalid or duplicate bundled model identities.'
    ),

    'diagnostic.codex_mcp.codex_app_server_closed_its_stdio_connection': (
        'Codex app-server closed its stdio connection.'
    ),
    'diagnostic.codex_mcp.codex_app_server_returned_an_invalid_protocol_message': (
        'Codex app-server returned an invalid protocol message.'
    ),
    'diagnostic.codex_mcp.codex_app_server_returned_invalid_notification_parameters': (
        'Codex app-server returned invalid notification parameters.'
    ),
    'diagnostic.codex_mcp.codex_app_server_returned_invalid_protocol_json': (
        'Codex app-server returned invalid protocol JSON.'
    ),
    'diagnostic.codex_mcp.codex_bootstrap_instructions_are_not_regular_home_configuration': (
        'Codex bootstrap instructions are not regular home configuration files.'
    ),
    'diagnostic.codex_mcp.codex_called_a_tool_outside_the_private_source': (
        'Codex called a tool outside the private source MCP server.'
    ),
    'diagnostic.codex_mcp.codex_cannot_disable_inherited_tools_before_thread_creation': (
        'Codex cannot disable inherited tools before thread creation.'
    ),
    'diagnostic.codex_mcp.codex_cannot_exclude_host_skill_discovery': 'Codex cannot exclude host skill discovery.',
    'diagnostic.codex_mcp.codex_cannot_safely_override_an_inherited_name': (
        'Codex cannot safely override an inherited {value} name.'
    ),
    'diagnostic.codex_mcp.codex_changed_the_required_private_source_mcp_configuration': (
        'Codex changed the required private source MCP configuration.'
    ),
    'diagnostic.codex_mcp.codex_completed_without_a_final_response': 'Codex completed without a final response.',
    'diagnostic.codex_mcp.codex_did_not_complete_the_mcp_analysis_turn': (
        'Codex did not complete the MCP analysis turn.'
    ),
    'diagnostic.codex_mcp.codex_did_not_create_an_isolated_thread_without': (
        'Codex did not create an isolated thread without a source environment.'
    ),
    'diagnostic.codex_mcp.codex_did_not_disable_every_required_native_tool': (
        'Codex did not disable every required native tool feature.'
    ),
    'diagnostic.codex_mcp.codex_did_not_identify_its_bootstrap_instruction_sources': (
        'Codex did not identify its bootstrap instruction sources.'
    ),
    'diagnostic.codex_mcp.codex_did_not_return_its_effective_configuration': (
        'Codex did not return its effective configuration.'
    ),
    'diagnostic.codex_mcp.codex_did_not_return_its_effective_mcp_server': (
        'Codex did not return its effective MCP server configuration.'
    ),
    'diagnostic.codex_mcp.codex_did_not_select_the_required_model_provider': (
        'Codex did not select the required model provider.'
    ),
    'diagnostic.codex_mcp.codex_exposed_an_unexpected_native_turn_item': (
        'Codex exposed an unexpected native turn item: {kind}.'
    ),
    'diagnostic.codex_mcp.codex_home_instructions_overlap_the_pinned_source_directory': (
        'Codex home instructions overlap the pinned source directory.'
    ),
    'diagnostic.codex_mcp.codex_loaded_project_instructions_outside_the_permitted_home': (
        'Codex loaded project instructions outside the permitted home configuration.'
    ),
    'diagnostic.codex_mcp.codex_rejected_the_required_app_server_operation': (
        'Codex rejected the required app-server operation: {method}.'
    ),
    'diagnostic.codex_mcp.codex_requested_an_interactive_operation_outside_the_mcp': (
        'Codex requested an interactive operation outside the MCP source tools.'
    ),
    'diagnostic.codex_mcp.codex_retained_authentication_or_network_settings_for_the': (
        'Codex retained authentication or network settings for the offline probe.'
    ),
    'diagnostic.codex_mcp.codex_retained_unsafe_source_mcp_environment_transport_or': (
        'Codex retained unsafe source MCP environment, transport, or output settings.'
    ),
    'diagnostic.codex_mcp.codex_retains_an_unexpected_mcp_server': 'Codex retains an unexpected MCP server.',
    'diagnostic.codex_mcp.codex_retains_automatic_skill_instructions': 'Codex retains automatic skill instructions.',
    'diagnostic.codex_mcp.codex_retains_enabled_app_or_plugin_configuration': (
        'Codex retains enabled app or plugin configuration.'
    ),
    'diagnostic.codex_mcp.codex_retains_external_instructions_outside_the_mcp_prompt': (
        'Codex retains external instructions outside the MCP prompt.'
    ),
    'diagnostic.codex_mcp.codex_retains_inherited_model_provider_configuration': (
        'Codex retains inherited model provider configuration.'
    ),
    'diagnostic.codex_mcp.codex_retains_native_agent_delegation': 'Codex retains native agent delegation.',
    'diagnostic.codex_mcp.codex_retains_notification_commands_or_hooks': (
        'Codex retains notification commands or hooks.'
    ),
    'diagnostic.codex_mcp.codex_retains_web_search_or_project_instruction_discovery': (
        'Codex retains web search or project instruction discovery.'
    ),
    'diagnostic.codex_mcp.codex_returned_an_invalid_bootstrap_instruction_path': (
        'Codex returned an invalid bootstrap instruction path.'
    ),
    'diagnostic.codex_mcp.codex_returned_an_invalid_turn_item': 'Codex returned an invalid turn item.',
    'diagnostic.codex_mcp.codex_returned_invalid_inherited_settings': (
        'Codex returned invalid inherited {value} settings.'
    ),
    'diagnostic.codex_mcp.codex_returned_unsupported_hook_trust_state': (
        'Codex returned unsupported hook trust state.'
    ),
    'diagnostic.codex_mcp.codex_selected_a_model_outside_the_pinned_catalog': (
        'Codex selected a model outside the pinned catalog.'
    ),
    'diagnostic.codex_mcp.codex_started_an_unexpected_mcp_server': 'Codex started an unexpected MCP server.',
    'diagnostic.codex_mcp.interactive_server_requests_are_disabled': 'Interactive server requests are disabled.',
    'diagnostic.codex_mcp.mcp_requires_the_pinned_catalog_with_native_model': (
        'MCP requires the pinned catalog with native model tools disabled.'
    ),
    'diagnostic.codex_mcp.mcp_transport_requires_absolute_private_artifact_paths_and': (
        'MCP transport requires absolute private artifact paths and a manifest hash.'
    ),
    'diagnostic.codex_mcp.required_mcp_server_explain_this_pls_failed_to': (
        'Required MCP server explain_this_pls failed to start.'
    ),
    'diagnostic.codex_mcp.server_error': 'Codex app-server: {error}',
    'diagnostic.codex_mcp.the_private_mcp_manifest_changed_before_thread_creation': (
        'The private MCP manifest changed before thread creation.'
    ),
    'diagnostic.codex_mcp.transport_failed': 'Codex MCP transport could not read its private artifacts or start.',
    'diagnostic.codex_mcp.turn_failed': 'Turn failed.',

    'diagnostic.codex_models.cannot_load_the_codex_model_catalog': 'Cannot load the Codex model catalog: {value}',
    'diagnostic.codex_models.codex_did_not_return_a_completed_nonempty_model': (
        'Codex did not return a completed, nonempty model response.'
    ),
    'diagnostic.codex_models.codex_model_catalog_contains_an_invalid_model_slug': (
        'Codex model catalog contains an invalid model slug.'
    ),
    'diagnostic.codex_models.codex_model_catalog_contains_invalid_or_duplicate_model': (
        'Codex model catalog contains invalid or duplicate model metadata.'
    ),
    'diagnostic.codex_models.codex_model_catalog_exceeded_its_second_time_limit': (
        'Codex model catalog exceeded its {timeout:g}-second time limit.'
    ),
    'diagnostic.codex_models.codex_model_catalog_must_contain_a_models_array': (
        'Codex model catalog must contain a models array.'
    ),
    'diagnostic.codex_models.codex_returned_an_invalid_model_catalog_json_document': (
        'Codex returned an invalid model catalog JSON document.'
    ),
    'diagnostic.codex_models.no_diagnostic': 'Codex returned no diagnostic.',
    'diagnostic.codex_models.sign_in_with_chatgpt_using_codex_login_then': (
        'Sign in with ChatGPT using codex login, then check this model again.'
    ),
    'diagnostic.codex_models.the_model_check_exceeded_its_second_time_limit': (
        'The model check exceeded its {timeout:g}-second time limit.'
    ),
    'diagnostic.codex_models.the_model_check_failed': 'The model check failed: {value}',
    'diagnostic.codex_models.the_model_returned_a_completed_response_using_chatgpt': (
        'The model returned a completed response using ChatGPT.'
    ),
    'diagnostic.codex_models.this_model_is_not_supported_with_your_codex': (
        'This model is not supported with your Codex CLI and ChatGPT account.'
    ),

    'diagnostic.config.agent_executable_must_be_a_string': 'agent.executable must be a string.',
    'diagnostic.config.agent_must_be_a_string_or_null': 'agent.{value} must be a string or null.',
    'diagnostic.config.agent_reasoning_effort_must_be_a_nonempty_string': (
        'agent.reasoning_effort must be a nonempty string or null.'
    ),
    'diagnostic.config.at_least_one_nonempty_local_branch_name_is': (
        'At least one nonempty local branch name is required.'
    ),
    'diagnostic.config.branches_must_be_unique_and_include_baseline_branch': (
        'Branches must be unique and include baseline_branch.'
    ),
    'diagnostic.config.configuration_must_be_a_json_object': 'Configuration must be a JSON object.',
    'diagnostic.config.configuration_requires_mode_git_or_folder': 'Configuration requires mode: "git" or "folder".',
    'diagnostic.config.continue_on_error_must_be_boolean': 'continue_on_error must be boolean.',
    'diagnostic.config.executable_was_not_found_install_it_separately': (
        '{backend} executable was not found. Install it separately.'
    ),
    'diagnostic.config.execution_must_be_a_json_object': 'execution must be a JSON object.',
    'diagnostic.config.last_analysis_finished_at_must_be_an_iso': (
        'last_analysis.finished_at must be an ISO 8601 timestamp with a timezone.'
    ),
    'diagnostic.config.last_analysis_finished_at_must_include_a_timezone': (
        'last_analysis.finished_at must include a timezone.'
    ),
    'diagnostic.config.last_analysis_must_contain_status_finished_at_and': (
        'last_analysis must contain status, finished_at and results_dir, or be null.'
    ),
    'diagnostic.config.last_analysis_results_dir_must_be_a_nonempty': (
        'last_analysis.results_dir must be a nonempty path string.'
    ),
    'diagnostic.config.last_analysis_status_must_be_completed_or_partial': (
        'last_analysis.status must be COMPLETED or PARTIAL.'
    ),
    'diagnostic.config.missing_configuration_key': 'Missing configuration key: {value}',
    'diagnostic.config.missing_configuration_section': 'Missing configuration section: {section}',
    'diagnostic.config.missing_prompt_template': 'Missing prompt template: {p}',
    'diagnostic.config.must_be_a_json_object': '{value} must be a JSON object.',
    'diagnostic.config.must_be_a_json_object_even_when_inactive': (
        '{section} must be a JSON object, even when inactive.'
    ),
    'diagnostic.config.must_be_a_nonempty_path_string': '{value} must be a nonempty path string.',
    'diagnostic.config.priority_scenarios_must_be_an_array_of_strings': (
        'priority_scenarios must be an array of strings.'
    ),
    'diagnostic.config.project_description_must_be_a_string': 'project_description must be a string.',
    'diagnostic.config.prompt_templates_must_be_outside_the_inspected_repository': (
        'Prompt templates must be outside the inspected repository.'
    ),
    'diagnostic.config.prompts_must_be_a_nonempty_path_string': 'prompts.{stage} must be a nonempty path string.',
    'diagnostic.config.remove_catalog_the_catalog_stage_has_been_removed': (
        'Remove {section}.catalog: the Catalog stage has been removed.'
    ),
    'diagnostic.config.result_policy_must_be_compromise_or_strict': 'result_policy must be compromise or strict.',
    'diagnostic.config.source_access_accepts_mode_only': 'source_access accepts mode only.',
    'diagnostic.config.source_access_mode_mcp_requires_source_context_enabled': (
        'source_access.mode=mcp requires source_context.enabled=true and mode=on-demand.'
    ),
    'diagnostic.config.source_access_mode_must_be_native_or_mcp': 'source_access.mode must be native or mcp.',
    'diagnostic.config.source_access_must_be_a_json_object': 'source_access must be a JSON object.',
    'diagnostic.config.source_context_must_be_a_json_object': 'source_context must be a JSON object.',
    'diagnostic.config.source_directory_and_reports_dir_must_be_disjoint': (
        'Source directory and reports_dir must be disjoint, not ancestors of each other.'
    ),
    'diagnostic.config.unknown_agent_configuration_field': 'Unknown agent configuration field.',
    'diagnostic.config.unknown_configuration_keys': 'Unknown configuration keys: {value}',
    'diagnostic.config.unknown_mode_keys': 'Unknown {mode}_mode keys: {value}',
    'diagnostic.config.unknown_prompt_stage': 'Unknown prompt stage.',
    'diagnostic.config.unknown_stage_override': 'Unknown stage override: {stage}',
    'diagnostic.config.unsupported_backend': 'Unsupported backend: {backend}',

    'diagnostic.config_edit.the_configuration_changed_use_refresh_before_saving': (
        'The configuration changed. Use Refresh before saving.'
    ),
    'diagnostic.config_edit.the_selected_configuration_section_must_be_an_object': (
        'The selected configuration section must be an object.'
    ),

    'diagnostic.contracts.claim_is_absent_from_the_frozen_registry': 'Claim is absent from the frozen registry.',
    'diagnostic.contracts.comparison_baseline_mismatch': 'Comparison baseline mismatch.',
    'diagnostic.contracts.comparison_conceals_inputs_failing_enabled_stage_checks': (
        'Comparison conceals inputs failing enabled-stage checks'
    ),
    'diagnostic.contracts.comparison_must_cover_each_non_baseline_branch_exactly': (
        'Comparison must cover each non-baseline branch exactly once'
    ),
    'diagnostic.contracts.comparison_reference_does_not_resolve_in_the_supplied': (
        'Comparison reference does not resolve in the supplied report'
    ),
    'diagnostic.contracts.complete_comparison_contains_unresolved_inputs': (
        'COMPLETE comparison contains unresolved inputs'
    ),
    'diagnostic.contracts.duplicate_json_key': 'Duplicate JSON key.',
    'diagnostic.contracts.evidence_references_the_wrong_comparison_side': (
        'Evidence references the wrong comparison side'
    ),
    'diagnostic.contracts.finding_references_a_claim_outside_the_frozen_registry': (
        'Finding references a claim outside the frozen registry.'
    ),
    'diagnostic.contracts.finding_requires_source_evidence': 'Finding requires source evidence',
    'diagnostic.contracts.hypothesis_unknown_requires_a_concrete_missing_check_or': (
        'Hypothesis/unknown requires a concrete missing check or limitation'
    ),
    'diagnostic.contracts.invalid_json': 'Invalid JSON syntax',
    'diagnostic.contracts.invalid_json_encoding_or_numeric_nesting_limit': (
        'Invalid JSON encoding or numeric/nesting limit.'
    ),
    'diagnostic.contracts.invalid_json_syntax_line_column_char': (
        'Invalid JSON syntax: line {lineno} column {colno} (char {pos}).'
    ),
    'diagnostic.contracts.invalid_jsonc_comment_line_column_char': (
        'Invalid JSONC comment: line {lineno} column {colno} (char {pos}).'
    ),
    'diagnostic.contracts.invalid_or_duplicate_claim_id': 'Invalid or duplicate claim ID.',
    'diagnostic.contracts.material_issue_lacks_a_linked_high_medium_finding': (
        'Material issue lacks a linked HIGH/MEDIUM finding.'
    ),
    'diagnostic.contracts.material_issue_requires_a_linked_material_finding': (
        'Material issue requires a linked material finding'
    ),
    'diagnostic.contracts.non_finite_json_value': 'Non-finite JSON value.',
    'diagnostic.contracts.outcome_is_incompatible_with_the_frozen_epistemic_kind': (
        'Outcome is incompatible with the frozen epistemic kind'
    ),
    'diagnostic.contracts.partial_blocked_requires_explicit_limitations': (
        'PARTIAL/BLOCKED requires explicit limitations'
    ),
    'diagnostic.contracts.rejected_type': (
        'Response contract rejected: {code} at {path}; expected {expected_type}, got {actual_type}.'
    ),
    'diagnostic.contracts.required_strings_must_not_be_empty_or_whitespace': (
        'Required strings must not be empty or whitespace'
    ),
    'diagnostic.contracts.response_contract_rejected_at': 'Response contract rejected: {code} at {path}.',
    'diagnostic.contracts.strong_contrast_requires_supported_factual_references_and_accepted': (
        'Strong contrast requires supported factual references and accepted inputs on both sides'
    ),
    'diagnostic.contracts.structured_result_has_schema_violation_s_first_at': (
        'Structured result has {total_violations} schema violation(s); first at {path}.'
    ),
    'diagnostic.contracts.supported_contradicted_assessments_require_source_evidence': (
        'Supported/contradicted assessments require source evidence'
    ),
    'diagnostic.contracts.the_agent_response_failed_contract_validation': (
        'The agent response failed contract validation.'
    ),
    'diagnostic.contracts.unassessed_or_insufficient_assessment_requires_a_limitation': (
        'Unassessed or insufficient assessment requires a limitation'
    ),
    'diagnostic.contracts.unfinished_omission_search_requires_a_limitation': (
        'Unfinished omission search requires a limitation'
    ),
    'diagnostic.contracts.unknown_difference_branch': 'Unknown difference branch',
    'diagnostic.contracts.unterminated_block_comment': 'Unterminated block comment',

    'diagnostic.coverage_plan.auxiliary_observation_does_not_match_its_legacy_schema': (
        'Auxiliary observation does not match its legacy schema.'
    ),
    'diagnostic.coverage_plan.auxiliary_observations_must_be_an_object': 'Auxiliary observations must be an object.',
    'diagnostic.coverage_plan.auxiliary_path_has_invalid_source_path_syntax': (
        'Auxiliary path has invalid source-path syntax.'
    ),
    'diagnostic.coverage_plan.auxiliary_path_is_absent_from_the_source_inventory': (
        'Auxiliary path is absent from the source inventory.'
    ),
    'diagnostic.coverage_plan.coverage_and_pinned_source_inventories_differ': (
        'Coverage and pinned source inventories differ.'
    ),
    'diagnostic.coverage_plan.coverage_plan_contains_an_unsafe_inventory_path': (
        'Coverage plan contains an unsafe inventory path.'
    ),
    'diagnostic.coverage_plan.coverage_plan_directory_groups_changed': 'Coverage plan directory groups changed.',
    'diagnostic.coverage_plan.coverage_plan_identifies_another_source_snapshot': (
        'Coverage plan identifies another source snapshot.'
    ),
    'diagnostic.coverage_plan.coverage_plan_inventory_groups_changed': 'Coverage plan inventory groups changed.',
    'diagnostic.coverage_plan.coverage_plan_membership_is_inconsistent': 'Coverage plan membership is inconsistent.',
    'diagnostic.coverage_plan.coverage_plan_unique_file_counts_changed': 'Coverage plan unique-file counts changed.',
    'diagnostic.coverage_plan.free_form_observations_are_retained_without_verifying_classification': (
        'Free-form observations are retained without verifying classification or paths.'
    ),
    'diagnostic.coverage_plan.frozen_coverage_plan_changed': 'Frozen coverage plan changed.',
    'diagnostic.coverage_plan.model_exclusion_cannot_change_the_source_inventory_only': (
        'Model exclusion cannot change the source inventory; only the user source filter can exclude sources.'
    ),
    'diagnostic.coverage_plan.no_pinned_inventory_is_available_to_verify_this': (
        'No pinned inventory is available to verify this path.'
    ),
    'diagnostic.coverage_plan.source_inventory_entries_in_this_directory_group_inspection': (
        'Source inventory entries in this directory group; inspection is unverified.'
    ),
    'diagnostic.coverage_plan.source_root': 'Source root',

    'diagnostic.errors.path': 'path {node_path!r}',
    'diagnostic.errors.required_sha': 'required SHA {required_commit}',
    'diagnostic.errors.snapshot': 'Snapshot {snapshot!r}',

    'diagnostic.evidence.evidence_bytes_differ_from_the_pinned_source_inventory': (
        'Evidence bytes differ from the pinned source inventory.'
    ),
    'diagnostic.evidence.pinned_evidence_requires_source_file_hashes': 'Pinned evidence requires source-file hashes.',
    'diagnostic.evidence.previously_read_source_path_became_n_a_or': (
        'Previously read source path became n/a or unsafe.'
    ),
    'diagnostic.evidence.previously_read_source_path_changed_during_evidence_resolution': (
        'Previously read source path changed during evidence resolution.'
    ),
    'diagnostic.evidence.source_changed_before_evidence_read': 'Source changed before evidence read.',
    'diagnostic.evidence.source_changed_during_evidence_resolution': 'Source changed during evidence resolution.',
    'diagnostic.evidence.source_component_changed_during_evidence_resolution': (
        'Source component changed during evidence resolution.'
    ),
    'diagnostic.evidence.source_component_disappeared_during_evidence_resolution': (
        'Source component disappeared during evidence resolution.'
    ),
    'diagnostic.evidence.source_grew_beyond_its_initial_file_size': 'Source grew beyond its initial file size.',
    'diagnostic.evidence.source_path_differs_from_the_pinned_snapshot_metadata': (
        'Source path differs from the pinned snapshot metadata.'
    ),

    'diagnostic.execution.another_architecture_audit_holds_this_repository_lock': (
        'Another architecture audit holds this repository lock.'
    ),
    'diagnostic.execution.execution_max_revision_rounds_must_be_or': 'execution.max_revision_rounds must be 0 or 1.',
    'diagnostic.execution.execution_must_be_an_object_containing_only_documented': (
        'execution must be an object containing only documented settings.'
    ),
    'diagnostic.execution.execution_must_be_boolean': 'execution.{value} must be boolean.',
    'diagnostic.execution.execution_structured_output_repair_attempts_must_be_an': (
        'execution.structured_output_repair_attempts must be an integer from 0 to 2.'
    ),
    'diagnostic.execution.execution_time_budget_exceeded': 'Execution time budget exceeded.',
    'diagnostic.execution.no_substantive_backend_activity_within_idle_budget': (
        'No substantive backend activity within idle budget.'
    ),
    'diagnostic.execution.positive_number': 'execution.{field} must be a positive finite number.',
    'diagnostic.execution.positive_number_or_null': 'execution.{field} must be a positive finite number or null.',
    'diagnostic.execution.unsafe_lock_directory': 'Unsafe lock directory',

    'diagnostic.folder.cannot_read_source_folder': 'Cannot read source folder {path}: {exc}',
    'diagnostic.folder.source_changed_while_fingerprinting': 'Source changed while fingerprinting: {name}',
    'diagnostic.folder.source_filter_changed_during_the': 'Source filter changed during the run.',
    'diagnostic.folder.source_folder_changed_during_the_files_will_not': (
        'Source folder changed during the run; files will not be restored.'
    ),
    'diagnostic.folder.source_folder_changed_from_the_pinned_snapshot': (
        'Source folder changed from the pinned snapshot.'
    ),
    'diagnostic.folder.source_folder_has_no_pinned_snapshot': 'Source folder has no pinned snapshot.',
    'diagnostic.folder.unsafe_path_in_source_folder': 'Unsafe path in source folder.',
    'diagnostic.folder.unsupported_special_file_in_source_folder': (
        'Unsupported special file in source folder: {child}'
    ),

    'diagnostic.git_sources.cannot_read_repository_ignore_configuration': (
        'Cannot read repository ignore configuration.'
    ),
    'diagnostic.git_sources.cannot_read_user_git_ignore_configuration': 'Cannot read user Git ignore configuration.',

    'diagnostic.invocation.codex_changed_its_verified_mcp_tool_schemas_during': (
        'Codex changed its verified MCP tool schemas during the model run.'
    ),
    'diagnostic.invocation.exited_with_inspect_private_attempt_logs': (
        '{backend} exited with {returncode}; inspect private attempt logs.'
    ),
    'diagnostic.invocation.frozen_evidence_package_cannot_be_replaced': 'Frozen evidence package cannot be replaced.',
    'diagnostic.invocation.mcp_detected_a_changed_pinned_source_or_private': (
        'MCP detected a changed pinned source or private artifact.'
    ),
    'diagnostic.invocation.mcp_receipts_do_not_belong_to_this_attempt': 'MCP receipts do not belong to this attempt.',
    'diagnostic.invocation.mcp_source_access_requires_the_prepared_on_demand': (
        'MCP source access requires the prepared on-demand source index.'
    ),
    'diagnostic.invocation.model_unknown': 'unknown: backend has not reported model identity',
    'diagnostic.invocation.model_usage_source': 'backend modelUsage; may include multiple models',
    'diagnostic.invocation.observed_mcp_receipts_disappeared_or_changed': (
        'Observed MCP receipts disappeared or changed.'
    ),
    'diagnostic.invocation.source_changed_during_context_preparation': 'Source changed during context preparation.',
    'diagnostic.invocation.source_changed_from_the_inventory_used_for_the': (
        'Source changed from the inventory used for the coverage plan.'
    ),
    'diagnostic.invocation.source_folder_changed_from_the_pinned_snapshot_before': (
        'Source folder changed from the pinned snapshot before invocation.'
    ),
    'diagnostic.invocation.the_configured_codex_agent_has_no_verified_mcp': (
        'The configured Codex agent has no verified MCP capability profile.'
    ),
    'diagnostic.invocation.the_report_author_used_a_tool_outside_its': (
        'The report author used a tool outside its evidence-only scope.'
    ),
    'diagnostic.invocation.verified_trace': 'verified Codex inference request trace',

    'diagnostic.ledger.configured_processing_checks_only_review_was_disabled_differences': (
        'Configured processing checks only; review was disabled. Differences are author-reported and '
        'unverified.'
    ),
    'diagnostic.ledger.frozen_document_registry_plan_identity_changed': (
        'Frozen document/registry/plan identity changed'
    ),
    'diagnostic.ledger.frozen_review_context_changed': 'Frozen review context changed',
    'diagnostic.ledger.policy_checks_concern_processing_and_agent_assessments_factual': (
        'Policy checks concern processing and agent assessments; factual correctness is not established.'
    ),
    'diagnostic.ledger.previous_material_findings_changed_or_were_omitted_from': (
        'Previous material findings changed or were omitted from the review context.'
    ),
    'diagnostic.ledger.required_frozen_review_plan_is_missing': 'Required frozen review plan is missing',

    'diagnostic.model_context.maps_root_must_be_an_absolute_path': 'maps_root must be an absolute path.',

    'diagnostic.pipeline.review_skipped_no_usable_architecture_document': (
        'Review skipped: no usable architecture document.'
    ),
    'diagnostic.pipeline.revised_study_unavailable': 'No usable strictly valid revised study.',
    'diagnostic.pipeline.stopped_after_retaining_unvalidated_material_continue_on_error': (
        'Stopped after retaining unvalidated material (continue_on_error=false).'
    ),
    'diagnostic.pipeline.study_unavailable': 'No usable architecture document.',
    'diagnostic.pipeline.text_retained_policy_checks_not_completed_agent_self': (
        '{message} Text retained; policy checks not completed. Agent self-assessment: {value}.'
    ),

    'diagnostic.preflight.check_the_configured_executable_and_its_version_help': (
        'Check the configured executable and its --version / --help locally.'
    ),
    'diagnostic.preflight.codex_cannot_report_independent_mcp_and_native_tool': (
        'Codex cannot report independent MCP and native tool capabilities.'
    ),
    'diagnostic.preflight.codex_selected_a_model_outside_its_pinned_catalog': (
        'Codex selected a model outside its pinned catalog.'
    ),
    'diagnostic.preflight.conflicting_expected_version_for_the_same_executable': (
        'Conflicting expected_version for the same executable.'
    ),
    'diagnostic.preflight.failed_its_cli_check_exit_code': '{backend} failed its CLI check: exit code {returncode}.',
    'diagnostic.preflight.lacks_required_cli_options': '{backend} lacks required CLI options: {required}',
    'diagnostic.preflight.no_cli_adapter_for_this_backend': 'No CLI adapter for this backend.',
    'diagnostic.preflight.version_differs_from_expected_version': '{backend} version differs from expected_version.',

    'diagnostic.process.could_not_finish_cleanup_of_the_owned_cli': (
        'Could not finish cleanup of the owned CLI process group.'
    ),
    'diagnostic.process.repeated_tool_results_produced_no_new_observable_material': (
        'Repeated tool results produced no new observable material.'
    ),

    'diagnostic.publication.computed_artifact_failed_local_validation': 'Computed artifact failed local validation.',
    'diagnostic.publication.frozen_stage_artifacts_cannot_be_replaced_select_a': (
        'Frozen stage artifacts cannot be replaced; select a new run directory.'
    ),
    'diagnostic.publication.published_document_registry_or_review_plan_changed': (
        'Published document, registry or review plan changed.'
    ),
    'diagnostic.publication.published_revision_artifact_changed': 'Published revision artifact changed.',
    'diagnostic.publication.recovered_material_cannot_replace_an_existing_frozen_registry': (
        'Recovered material cannot replace an existing frozen registry or plan.'
    ),
    'diagnostic.publication.selected_revision_artifact_changed_during_publication': (
        'Selected revision artifact changed during publication.'
    ),

    'diagnostic.repository.a_clean_checkout_is_required_staged_tracked_or': (
        'A clean checkout is required: staged, tracked, or non-ignored untracked changes exist.'
    ),
    'diagnostic.repository.a_normal_standalone_checkout_is_required_no_bare': (
        'A normal standalone checkout is required; no bare/linked worktree.'
    ),
    'diagnostic.repository.assume_unchanged_worktree_entries_are_unsupported': (
        'assume-unchanged/skip-worktree entries are unsupported.'
    ),
    'diagnostic.repository.cannot_determine_git_version_at_git_is_required': (
        'Cannot determine Git version at {executable}: {version_string!r}; Git >= 2.34.1 is required.'
    ),
    'diagnostic.repository.cannot_express_exact_safe_directory_trust_for_a': (
        'Cannot express exact safe.directory trust for a checkout named "*": {path}'
    ),
    'diagnostic.repository.cannot_parse_committed_gitmodules': 'Cannot parse committed .gitmodules.',
    'diagnostic.repository.cannot_read_head_at': 'Cannot read HEAD at {path}: {exc}',
    'diagnostic.repository.checkout_did_not_reach_the_expected_head': 'Checkout did not reach the expected HEAD.',
    'diagnostic.repository.conflicted_git_index': 'Conflicted Git index.',
    'diagnostic.repository.directory_replaced_or_symbolic_link_encountered': (
        'Directory replaced or symbolic link encountered: {item}'
    ),
    'diagnostic.repository.duplicate_submodule_path_name': 'Duplicate submodule path/name: {path!r}, {name!r}',
    'diagnostic.repository.empty_or_oversized_head': 'Empty or oversized HEAD: {path}',
    'diagnostic.repository.external_object_alternates_are_unsupported_make_objects_local': (
        'External object alternates are unsupported; make objects local first.'
    ),
    'diagnostic.repository.found_at_git_is_required': (
        'Found {version_string} at {executable}; Git >= 2.34.1 is required.'
    ),
    'diagnostic.repository.git_data_does_not_belong_to_expected_checkout': (
        'Git data does not belong to expected checkout: {path}'
    ),
    'diagnostic.repository.git_directory_reused_by_multiple_submodules': (
        'Git directory reused by multiple submodules.'
    ),
    'diagnostic.repository.git_failed': 'git {value} failed: {error}',
    'diagnostic.repository.git_filters_including_git_lfs_filters_require_a': (
        'Git filters (including Git LFS filters) require a separate supported policy.'
    ),
    'diagnostic.repository.git_is_required_for_git_mode_git_was': (
        'Git >= 2.34.1 is required for git mode; git was not found in PATH.'
    ),
    'diagnostic.repository.git_metadata_must_be_a_regular_file': 'Git metadata must be a regular file: {path}',
    'diagnostic.repository.gitlinks_and_committed_gitmodules_paths_differ': (
        'gitlinks and committed .gitmodules paths differ: {value}'
    ),
    'diagnostic.repository.gitmodules_must_be_a_regular_tracked_file': '.gitmodules must be a regular tracked file.',
    'diagnostic.repository.head_changed_while_opening': 'HEAD changed while opening: {path}',
    'diagnostic.repository.head_changed_while_reading': 'HEAD changed while reading: {path}',
    'diagnostic.repository.head_index_changed_outside_the_orchestrator': (
        'HEAD/index changed outside the orchestrator.'
    ),
    'diagnostic.repository.head_index_or_ignore_rules_changed_before_checkout': (
        'HEAD/index or ignore rules changed before checkout.'
    ),
    'diagnostic.repository.head_must_be_a_regular_file_not_a': 'HEAD must be a regular file, not a symlink: {path}',
    'diagnostic.repository.ignore_rules_changed_before_checkout': 'Ignore rules changed before checkout.',
    'diagnostic.repository.invalid_detached_head_at': 'Invalid detached HEAD at {value}',

    'diagnostic.repository.invalid_detached_head_at.symbolic_ref': 'Invalid detached HEAD at {value}: {exc}',

    'diagnostic.repository.invalid_git_file': 'Invalid .git file: {entry}',
    'diagnostic.repository.invalid_object_id_for_format': 'Invalid object ID for format {object_format!r}',
    'diagnostic.repository.invalid_symbolic_head_at': 'Invalid symbolic HEAD at {value}',

    'diagnostic.repository.invalid_symbolic_head_at.symbolic_ref': 'Invalid symbolic HEAD at {value}: {ref!r}',

    'diagnostic.repository.invalid_symbolic_head_at.symbolic_ref.template': (
        'Invalid symbolic HEAD at {value}: {ref!r}: {exc}'
    ),

    'diagnostic.repository.linked_common_git_directories_are_unsupported': (
        'Linked/common Git directories are unsupported.'
    ),
    'diagnostic.repository.object_is_not_a_commit': 'Object is not a commit',
    'diagnostic.repository.original_branch_moved_during_the': 'Original branch moved during the run.',
    'diagnostic.repository.ownership_hint': (
        "\nGit rejected the repository because of an ownership mismatch.\nRun with the checkout owner's UID, "
        'or use --trust-repository\nonly if you trust this specific checkout.'
    ),
    'diagnostic.repository.partial_clone_promisor_configuration_is_unsupported_git_may': (
        'Partial-clone/promisor configuration is unsupported: Git may attempt lazy fetching and change '
        'config before transport denial. Prepare a full local checkout.'
    ),
    'diagnostic.repository.path_escapes_checkout_boundary': 'Path escapes checkout boundary: {path}',
    'diagnostic.repository.repository_ownership_mismatch_at_owner_uid_runner_uid': (
        'Repository ownership mismatch at {path}: owner UID {owner}, runner UID {value}. Use '
        '--trust-repository only if you trust this checkout.'
    ),
    'diagnostic.repository.required_object_is_not_a_commit': 'Required object is not a commit.',
    'diagnostic.repository.required_tree_blob_objects_are_n_a_locally': 'Required tree/blob objects are n/a locally.',
    'diagnostic.repository.sparse_checkouts_are_unsupported': 'Sparse checkouts are unsupported.',
    'diagnostic.repository.submodule_head_differs_from_its_parent_gitlink': (
        'Submodule HEAD differs from its parent gitlink.'
    ),
    'diagnostic.repository.symbolic_git_entry': 'Symbolic .git entry: {entry}',
    'diagnostic.repository.the_submodule_is_not_initialized_locally': 'The submodule is not initialized locally.',
    'diagnostic.repository.unexpected_external_git_directory_for': 'Unexpected external Git directory for {path}',
    'diagnostic.repository.unexpected_git_diff_raw_z_layout': 'Unexpected git diff --raw -z layout',
    'diagnostic.repository.unfinished_git_operation': 'Unfinished Git operation: {marker}',
    'diagnostic.repository.unsafe_submodule_path_or_name': 'Unsafe submodule path or name: {value!r}',
    'diagnostic.repository.unsupported_submodule_structure_change_at_required_sha_paths': (
        'Unsupported submodule structure change at {path}/{child}: required SHA {commit}; paths and logical '
        'names must match original checkout.'
    ),
    'diagnostic.repository.unsupported_symbolic_head': 'Unsupported symbolic HEAD: {ref}',
    'diagnostic.repository.working_directory_or_git_metadata_changed_outside_the': (
        'Working directory or Git metadata changed outside the orchestrator.'
    ),

    'diagnostic.runner.acceptance_meaning': (
        'accepted=true means study and review policy checks satisfied; factual correctness is not '
        'established.'
    ),
    'diagnostic.runner.could_not_save_restoration_journal': 'Could not save restoration journal: {message}',
    'diagnostic.runner.folder_integrity': (
        'One initial content fingerprint; metadata checked at stage boundaries. Not a backup or continuous '
        'immutability guarantee.'
    ),
    'diagnostic.runner.original_checkout_restoration_failed': 'Original checkout restoration failed: {error}',
    'diagnostic.runner.source_check_meaning': (
        'MATCHED_AT_BOUNDARIES means pinned source metadata matched at performed checks only.'
    ),
    'diagnostic.runner.source_preservation': (
        'Only the orchestrator switches checkouts; agents have read-only source access.'
    ),

    'diagnostic.server.mcp_sdk_is_required': 'MCP SDK 1.26.0 is required.',

    'diagnostic.service.duplicate_mcp_source_file': 'Duplicate MCP source file.',
    'diagnostic.service.invalid_source_tool_arguments': 'Invalid source tool arguments.',
    'diagnostic.service.mcp_artifact_and_parent_directory_must_be_private': (
        'MCP artifact and parent directory must be private.'
    ),
    'diagnostic.service.mcp_artifact_path_must_be_absolute': 'MCP artifact path must be absolute.',
    'diagnostic.service.mcp_file_identity_does_not_match_the_inventory': (
        'MCP file identity does not match the inventory.'
    ),
    'diagnostic.service.mcp_files_do_not_match_the_pinned_inventory': 'MCP files do not match the pinned inventory.',
    'diagnostic.service.mcp_index_does_not_match_the_pinned_inventory': (
        'MCP index does not match the pinned inventory.'
    ),
    'diagnostic.service.mcp_index_is_incomplete': 'MCP index is incomplete.',
    'diagnostic.service.mcp_manifest_path_must_be_absolute': 'MCP manifest path must be absolute.',
    'diagnostic.service.mcp_private_artifact_integrity_check_failed': 'MCP private artifact integrity check failed.',
    'diagnostic.service.mcp_private_attempt_and_source_directory_must_be': (
        'MCP private attempt and source directory must be disjoint.'
    ),
    'diagnostic.service.mcp_receipt_file_must_be_private_and_must': (
        'MCP receipt file must be private and must not be linked.'
    ),
    'diagnostic.service.mcp_source_root_must_be_absolute': 'MCP source root must be absolute.',
    'diagnostic.service.mcp_source_snapshot_identity_is_invalid': 'MCP source snapshot identity is invalid.',
    'diagnostic.service.mcp_symbol_map_belongs_to_a_different_source': (
        'MCP symbol map belongs to a different source file.'
    ),
    'diagnostic.service.mcp_symbol_map_differs_from_the_prepared_declarations': (
        'MCP symbol map differs from the prepared declarations.'
    ),
    'diagnostic.service.mcp_symbol_map_is_outside_the_private_attempt': (
        'MCP symbol map is outside the private attempt.'
    ),
    'diagnostic.service.mcp_symbol_map_reference_is_missing': 'MCP symbol map reference is missing.',
    'diagnostic.service.one_result_exceeds_the_response_limit': 'One result exceeds the response limit.',
    'diagnostic.service.original_source_bytes_differ_from_the_pinned_hash': (
        'Original source bytes differ from the pinned hash.'
    ),
    'diagnostic.service.pinned_source_or_private_artifact_changed': 'Pinned source or private artifact changed.',
    'diagnostic.service.source_decoding_failed': 'Source decoding failed.',
    'diagnostic.service.source_read_failed': 'Source read failed.',
    'diagnostic.service.source_tool_request_cannot_be_served': 'Source tool request cannot be served.',

    'diagnostic.session.all_branches': 'all branches',
    'diagnostic.session.backend_usage_metadata_may_include_multiple_models': (
        'backend usage metadata; may include multiple models'
    ),
    'diagnostic.session.trust_repository_requires_git_mode_it_cannot_be': (
        '--trust-repository requires git mode; it cannot be used in folder mode.'
    ),
    'diagnostic.session.unsupported_artifact_format_select_a_new_directory': (
        'Unsupported artifact format; select a new run directory.'
    ),

    'diagnostic.source_context.prepared_source_bytes_differ_from_the_pinned_source': (
        'Prepared source bytes differ from the pinned source inventory.'
    ),
    'diagnostic.source_context.source_context_accepts_enabled_and_mode_only': (
        'source_context accepts enabled and mode only.'
    ),
    'diagnostic.source_context.source_context_enabled_must_be_a_json_boolean': (
        'source_context.enabled must be a JSON boolean.'
    ),
    'diagnostic.source_context.source_context_inventory_differs_from_the_pinned_file': (
        'Source context inventory differs from the pinned file hashes.'
    ),
    'diagnostic.source_context.source_context_mode_must_be_full_or_on': (
        'source_context.mode must be full or on-demand.'
    ),
    'diagnostic.source_context.source_context_requires_the_pinned_inventory': (
        'Source context requires the pinned inventory.'
    ),
    'diagnostic.source_context.unsafe_file_in_pinned_source_context_inventory': (
        'Unsafe file in pinned source context inventory.'
    ),

    'diagnostic.source_decoding.duplicate_source_decoding_rule_path': 'Duplicate source_decoding rule path.',
    'diagnostic.source_decoding.each_source_decoding_rule_requires_path_and_encoding': (
        'Each source_decoding rule requires path and encoding only.'
    ),
    'diagnostic.source_decoding.source_decoding_must_contain_only_a_rules_array': (
        'source_decoding must contain only a rules array.'
    ),
    'diagnostic.source_decoding.source_decoding_rule_paths_must_be_safe_root': (
        'source_decoding rule paths must be safe root-relative paths or ".".'
    ),
    'diagnostic.source_decoding.unsupported_source_decoding_encoding': 'Unsupported source_decoding encoding.',

    'diagnostic.source_filter.duplicate_source_filter_exclude_paths_entry': (
        'Duplicate source_filter.exclude_paths entry.'
    ),
    'diagnostic.source_filter.source_filter_exclude_paths_must_be_an_array': (
        'source_filter.exclude_paths must be an array of relative paths.'
    ),
    'diagnostic.source_filter.source_filter_exclude_paths_requires_safe_root_relative': (
        'source_filter.exclude_paths requires safe root-relative paths without glob patterns.'
    ),
    'diagnostic.source_filter.source_filter_follow_gitignore_must_be_a_json': (
        'source_filter.follow_gitignore must be a JSON boolean.'
    ),
    'diagnostic.source_filter.source_filter_must_contain_only_follow_gitignore_and': (
        'source_filter must contain only follow_gitignore and exclude_paths.'
    ),

    'diagnostic.study_normalization.evidence_normalization_requires_study_or_review': (
        'Evidence normalization requires study or review'
    ),

    'diagnostic.tool_observer.codex_model_request_events_are_unavailable': (
        'Codex model request events are unavailable.'
    ),
    'diagnostic.tool_observer.codex_shell_source_coordinates_and_model_request_events': (
        'Codex shell source coordinates and model request events are unavailable.'
    ),
    'diagnostic.tool_observer.counts_contain_only_observed_completed_native_calls': (
        'Counts contain only observed completed native calls.'
    ),
    'diagnostic.tool_observer.incomplete_utf_in_tool_event_stream': 'Incomplete UTF-8 in tool event stream.',
    'diagnostic.tool_observer.invalid_prepared_fragment_delivery_metadata': (
        'Invalid prepared fragment delivery metadata.'
    ),
    'diagnostic.tool_observer.invalid_utf_in_tool_event_stream': 'Invalid UTF-8 in tool event stream.',
    'diagnostic.tool_observer.malformed_or_unsupported_json_event': 'Malformed or unsupported JSON event.',
    'diagnostic.tool_observer.native_tool_call_identity_unavailable': 'Native tool call identity unavailable.',
    'diagnostic.tool_observer.no_terminal_backend_event_observed': 'No terminal backend event observed.',
    'diagnostic.tool_observer.nonmonotonic_event_observation_time': 'Nonmonotonic event observation time.',
    'diagnostic.tool_observer.only_explicit_source_coordinates_from_verified_completed_tool': (
        'Only explicit source coordinates from verified completed tool results are counted.'
    ),
    'diagnostic.tool_observer.private_server_receipts_not_model_inspection': (
        'Private server receipts; not model inspection.'
    ),
    'diagnostic.tool_observer.repeated_completed_tool_results_without_new_observable_material': (
        'Repeated completed tool results without new observable material.'
    ),
    'diagnostic.tool_observer.source_mcp_call_failed_before_a_verified_result': (
        'Source MCP call failed before a verified result was observed.'
    ),
    'diagnostic.tool_observer.source_mcp_result_differs_from_the_pinned_inventory': (
        'Source MCP result differs from the pinned inventory.'
    ),
    'diagnostic.tool_observer.source_mcp_result_has_an_unexplained_protocol_error': (
        'Source MCP result has an unexplained protocol error.'
    ),
    'diagnostic.tool_observer.source_mcp_result_has_no_matching_private_served': (
        'Source MCP result has no matching private SERVED receipt.'
    ),
    'diagnostic.tool_observer.source_mcp_result_identity_does_not_match_this': (
        'Source MCP result identity does not match this attempt.'
    ),
    'diagnostic.tool_observer.source_mcp_result_is_incomplete_or_has_an': (
        'Source MCP result is incomplete or has an unknown envelope.'
    ),
    'diagnostic.tool_observer.source_mcp_result_or_its_private_receipt_could': (
        'Source MCP result or its private receipt could not be verified.'
    ),
    'diagnostic.tool_observer.source_mcp_result_text_is_not_a_json': 'Source MCP result text is not a JSON object.',
    'diagnostic.tool_observer.source_mcp_result_text_was_not_observed': 'Source MCP result text was not observed.',
    'diagnostic.tool_observer.source_mcp_returned_an_unknown_source_locator': (
        'Source MCP returned an unknown source locator.'
    ),
    'diagnostic.tool_observer.source_mcp_returned_invalid_source_coordinates_or_text': (
        'Source MCP returned invalid source coordinates or text metadata.'
    ),
    'diagnostic.tool_observer.source_mcp_source_text_does_not_match_its': (
        'Source MCP source text does not match its returned line range.'
    ),
    'diagnostic.tool_observer.source_mcp_text_differs_from_its_structured_result': (
        'Source MCP text differs from its structured result.'
    ),
    'diagnostic.tool_observer.tool_calls_without_completed_results': 'Tool calls without completed results.',
    'diagnostic.tool_observer.unsupported_backend_event_format': 'Unsupported backend event format.',
    'diagnostic.tool_observer.unsupported_completed_codex_item_type': 'Unsupported completed Codex item type.',

    'diagnostic.validation.program_document_assembly_failed_its_invariant_checks': (
        'Program document assembly failed its invariant checks.'
    ),
    'diagnostic.validation.source_changed_during_citation_correction': 'Source changed during citation correction.',

    'installer.cannot_reuse_it_is_not_a_complete_virtual': (
        'Cannot reuse {environment}: it is not a complete virtual environment. Move it aside and run the '
        'installer again.'
    ),
    'installer.creating': 'Creating {environment}',
    'installer.description': 'Prepare a local virtual environment and install explain-this-pls requirements.',
    'installer.installation_complete_start_explain_this_pls_with': (
        '\nInstallation complete. Start explain-this-pls with:\n{value}'
    ),
    'installer.installation_failed': 'Installation failed: {exc}',
    'installer.installation_interrupted_the_installer_again_to_retry': (
        '\nInstallation interrupted. Run the installer again to retry.'
    ),
    'installer.installation_requires_macos_or_linux_and_python': (
        'Installation requires macOS or Linux and Python 3.11+.'
    ),
    'installer.installing_requirements_from': 'Installing requirements from {requirements}',
    'installer.python_required': 'The existing .venv needs Python 3.11+. Move it aside and rerun the installer.',
    'installer.requirements_file_is_missing': 'Requirements file is missing: {requirements}',
    'installer.reusing': 'Reusing {environment}',

    'review.summary.accepted.en': 'Review completed and accepted under policy checks.',
    'review.summary.accepted.ru': 'Ревью завершено и принято по проверкам политики.',

    'review.summary.disabled.en': 'Review is disabled by configuration, no separate review was performed.',
    'review.summary.disabled.ru': 'Ревью отключено конфигурацией, отдельная проверка не выполнена.',

    'review.summary.incomplete.en': 'Review is incomplete, no positive policy acceptance.',
    'review.summary.incomplete.ru': 'Ревью не завершено, положительная приёмка отсутствует.',

    'review.summary.issues.en': 'Review reported material findings, acceptance has not passed.',
    'review.summary.issues.ru': 'Ревью обнаружило существенные замечания, приёмка не пройдена.',

    'review.summary.not_accepted.en': 'Review completed, policy acceptance has not passed.',
    'review.summary.not_accepted.ru': 'Ревью завершено, приёмка по проверкам политики не пройдена.',

    'run.agent_estimated_cost_attempts': '      Agent-estimated cost: {value} | Attempts: {attempts} | {other_value}',
    'run.ai_service_access_and_model_availability_were_not': (
        'AI service access and model availability were not checked.'
    ),
    'run.an_unexpected_internal_error_stopped_the': 'An unexpected internal error stopped the run.',
    'run.analysis_complete': 'Analysis complete.',
    'run.analysis_complete_reports_may_still_contain_errors': (
        '[OK] Analysis complete. Reports may still contain errors.'
    ),
    'run.analysis_complete_with_citation_warnings_check_affected_claims': (
        '[WARN] Analysis complete with citation warnings. Check affected claims and the recorded acceptance '
        'result.'
    ),
    'run.analysis_failed': '[FAIL] Analysis failed.',
    'run.analysis_has_not_started': 'Analysis has not started.',
    'run.analysis_interrupted': '[WARN] Analysis interrupted.',
    'run.analysis_may_be_incomplete_see_available_results_and': (
        '[WARN] Analysis may be incomplete. See available results and limitations below.'
    ),
    'run.analyzing_project': 'Analyzing project…',
    'run.architecture_report_created': 'Architecture report created.',
    'run.architecture_revision': 'Architecture revision',
    'run.branch': 'Branch {branch}: ',

    'run.branch.finish': 'Branch {branch}: {value}',

    'run.branch_report_comparison': 'Branch report comparison',
    'run.branch_report_comparison_ready': 'Branch report comparison ready.',
    'run.cache_read_write_reasoning': (
        '      Cache read/write: {value} / {other_value} | Reasoning: {other_other_value}'
    ),
    'run.check_the_path_available_disk_space_and_access': (
        'Check the path, available disk space and access permissions.'
    ),
    'run.checking_local_setup': 'Checking local setup…',

    'run.checking_local_setup.render': 'Checking local setup.',

    'run.citation_warning': (
        'Citation {evidence_id}: {source} / {path}:{start_line}-{end_line}; {reason}; {action}; affected '
        'claims: {claims}. Check citation support for these claims.'
    ),
    'run.citation_warning_with_original': (
        'Citation {evidence_id}: {source} / {path}:{start_line}-{end_line}; {reason}; {action}; affected '
        'claims: {claims}; original lines: {original_start_line}-{original_end_line}. Check citation support '
        'for these claims.'
    ),
    'run.code': 'Code: {code}',
    'run.comparing_branch_reports': 'Comparing branch reports…',
    'run.complete': 'Complete',
    'run.complete_with_citation_warnings_see_affected_claims': (
        '{title} complete with citation warnings. See affected claims.'
    ),
    'run.complete_with_warnings': 'Complete with warnings',
    'run.could_not_prepare_analysis': 'Could not prepare analysis.',
    'run.could_not_remove_temporary_analysis_data': 'Could not remove temporary analysis data.',
    'run.could_not_restore_the_original_checkout': 'Could not restore the original checkout.',
    'run.detail': '[RUN] Detail: {event} {value}',
    'run.diagnostic_details': 'Diagnostic details:',
    'run.elapsed': '      Elapsed: {value}{last}',

    'run.elapsed.finish': 'Elapsed: {value}',
    'run.elapsed.render': '{label}{value} | Elapsed: {other_value}',

    'run.error_details': 'Error details:',
    'run.error_details_could_not_be_saved_analysis_will': (
        '[WARN] Error details could not be saved. Analysis will continue.'
    ),
    'run.error_summary': 'Error summary',
    'run.estimated': 'estimated',
    'run.failed': 'Failed',
    'run.input_or_cli_output_could_not_be_decoded': 'Input or CLI output could not be decoded.',
    'run.inspect_the_private_attempt_artifacts_listed_in_invocation': (
        'Inspect the private attempt artifacts listed in invocation.json.'
    ),
    'run.inspect_the_technical_log_and_report_this_error': (
        'Inspect the technical log and report this error with its run ID.'
    ),
    'run.invalid_json_syntax': 'Invalid JSON syntax.',
    'run.last_cli_output_ago': 'Last CLI output: {value} ago',
    'run.local_command_exceeded_its_time_limit': 'Local command exceeded its time limit.',
    'run.local_setup_checked_analysis_has_not_started': '[OK] Local setup checked. Analysis has not started.',
    'run.may_be_incomplete': 'May be incomplete',
    'run.model_n_a': 'model n/a',
    'run.next_step': 'Next step:',
    'run.no_activity_was_detected_within_the_time_limit': 'No activity was detected within the time limit.',
    'run.no_checkout_switches_were_performed': ' No checkout switches were performed.',
    'run.no_cli_output_received_yet': 'No CLI output received yet',
    'run.no_model_calls': 'No model calls',
    'run.no_model_calls_were_made': 'No model calls were made.',
    'run.not_started': 'Not started',
    'run.original_checkout_hierarchy_restored': '[OK] Original checkout hierarchy restored.',
    'run.original_checkout_hierarchy_was_not_fully_restored_see': (
        '[FAIL] Original checkout hierarchy was not fully restored. See diagnostics.'
    ),
    'run.partial': 'partial',
    'run.path': 'Path',
    'run.prepare_this_submodule_and_check_again_initialization_may': (
        'Prepare this submodule and run --check again.\nInitialization may download data and change its '
        'checkout.'
    ),
    'run.preparing_analysis': 'Preparing analysis.',
    'run.project_analysis': 'Project analysis',
    'run.project_description_is_missing_set_project_description_to': (
        '[WARN] Project description is missing. Set project_description to describe the system purpose and '
        'history.'
    ),
    'run.reason': 'Reason:',
    'run.report': '      Report: {report_path}',

    'run.report.finish': 'Report ({branch})',

    'run.report.finish.label': 'Report',

    'run.report_review': 'Report review',
    'run.report_text_saved_but_it_did_not_pass': 'Report text saved, but it did not pass all checks.',
    'run.required_commit': 'Required commit',
    'run.restoring_the_original_checkout_hierarchy': '[RUN] Restoring the original checkout hierarchy.',
    'run.review_disabled_by_configuration_no_separate_review_performed': (
        'Review disabled by configuration; no separate review performed.'
    ),
    'run.review_response_saved_see_acceptance_and_findings': 'Review response saved. See acceptance and findings.',
    'run.reviewing_report': 'Reviewing report…',
    'run.revised_architecture_report_created': 'Revised architecture report created.',
    'run.revising_architecture_report': 'Revising architecture report…',
    'run.running_with_administrator_privileges': '[WARN] Running with administrator privileges.',
    'run.skipped_no_architecture_report_available': '{title} skipped: no architecture report available.',
    'run.snapshot': 'Snapshot',
    'run.source': 'Source: {source}',
    'run.stage_error': '{title} failed.',
    'run.stage_failed': '{title} failed. See details below.',
    'run.stage_incomplete': '{title} may be incomplete. See details below.',
    'run.stage_status_elapsed_attempts_tokens_agent_estimated_cost': (
        'Stage | Status | Elapsed | Attempts | Tokens | Agent-estimated cost | Agent / model'
    ),
    'run.start_analysis': 'Start analysis:',
    'run.stopping_analysis': '[WARN] Stopping analysis…',
    'run.submodule': 'Submodule',
    'run.the_agent_could_not_be_stopped_or_cleaned': 'The agent could not be stopped or cleaned up completely.',
    'run.the_architecture_report_could_not_be_created': 'The architecture report could not be created.',
    'run.the_operation_exceeded_its_time_limit': 'The operation exceeded its time limit.',
    'run.the_report_could_not_be_saved_because_it': 'The report could not be saved because it failed validation.',
    'run.the_response_could_not_be_used': 'The response could not be used.',
    'run.the_was_interrupted': 'The run was interrupted.',
    'run.tokens_agent_estimated_cost_attempts': (
        '      Tokens: {value} | Agent-estimated cost: {other_value} | Attempts: {attempts} | '
        '{other_other_value}'
    ),
    'run.tokens_input_output': '      Tokens: {value} | Input: {other_value} | Output: {other_other_value}',
    'run.total_usage': 'Total usage:',
    'run.use_verbose_for_details': 'Use --verbose for details.',
    'run.use_verbose_for_details_and_report_this_error': 'Use --verbose for details and report this error.',
    'run.writing_the_architecture_report_from_the_frozen_evidence': (
        'Writing the architecture report from the frozen evidence package.'
    ),

    'tui.access_via_api': 'Access via API',
    'tui.access_via_chatgpt': 'Access via ChatGPT',
    'tui.agent_model': 'Agent / model: {value}',
    'tui.analysis_failed': 'Analysis failed.',
    'tui.analysis_interrupted': 'Analysis interrupted.',
    'tui.analysis_interrupted_before_starting': 'Analysis interrupted before starting.',
    'tui.attempts': 'Attempts',
    'tui.available': 'Available',
    'tui.cancel': 'Cancel',
    'tui.cannot_read_the_config_folder': 'Cannot read the config folder: {exc}',
    'tui.cannot_start': 'Cannot start: {error}',
    'tui.cannot_use_saved_settings': 'Cannot use saved settings: {exc}',
    'tui.check_chatgpt_access': 'Check ChatGPT Access',
    'tui.check_config': 'Check config',
    'tui.checking_model_access_with_chatgpt': 'Checking model access with ChatGPT…',
    'tui.choose_an_agent_model_field': 'Choose an agent model field.',
    'tui.choose_an_existing_directory': 'Choose an existing directory.',
    'tui.choose_the_folder_containing_your_json_jsonc_configs': (
        'Choose the folder containing your JSON/JSONC configs.'
    ),
    'tui.cli_default': 'CLI default',
    'tui.collecting_claims_and_source_evidence': 'Collecting claims and source evidence.',
    'tui.completed_with_citation_warnings_see_affected_claims': (
        'Completed with citation warnings. See affected claims.'
    ),
    'tui.config': 'Config: {path}',

    'tui.config.on_mount': 'Config',
    'tui.config.refresh_configs': 'Config: {explicit_config}',
    'tui.config.select_config': 'Config: {selected}',

    'tui.config_folder': 'Config folder',

    'tui.config_folder.refresh_configs': 'Config folder: {directory}',

    'tui.config_folder_path': 'Config folder path',
    'tui.configs': 'Configs',
    'tui.cost': 'Cost',
    'tui.could_not_start_analysis': 'Could not start analysis: {exc}',
    'tui.current_executable': 'Current: {value} | Executable: {executable}',
    'tui.diagnostic': 'Diagnostic {value}',

    'tui.diagnostic.code': 'Code',
    'tui.diagnostic.details': 'Details',
    'tui.diagnostic.failure_kind': 'Failure Kind',
    'tui.diagnostic.failure_layer': 'Failure Layer',
    'tui.diagnostic.hint': 'Hint',
    'tui.diagnostic.message': 'Message',
    'tui.diagnostic.node_path': 'Node Path',

    'tui.disabled_no_separate_review': 'Disabled; no separate review.',
    'tui.effort': 'Effort',
    'tui.elapsed': 'Elapsed',
    'tui.enabled_may_revise_and_review_again': 'Enabled; may revise and review again.',
    'tui.enter_a_config_folder_path': 'Enter a config folder path.',
    'tui.explain_config_must_contain_a_config_dir_path': 'explain.config must contain a config_dir path.',
    'tui.final_report': 'Final report',
    'tui.final_result': 'Final result',
    'tui.finished': 'Finished: {finished}',
    'tui.global': 'global',
    'tui.go': 'Go',
    'tui.invalid': 'Invalid',
    'tui.item': 'Item',
    'tui.loading_codex_model_catalog': 'Loading Codex model catalog…',
    'tui.local_setup_prepared': 'Local setup prepared.',
    'tui.manifest': 'Manifest',
    'tui.meaning': 'Meaning',
    'tui.missing': 'Missing',
    'tui.mode': 'Mode',
    'tui.models': 'Models',
    'tui.n_a': 'n/a',
    'tui.no_architecture_report_available': 'No architecture report available.',
    'tui.no_configs_found_copy_an_example_config_into': (
        'No configs found. Copy an example config into this folder, edit its paths, then Refresh.'
    ),
    'tui.no_report_available': 'No report available',
    'tui.not_checked': 'Not checked',
    'tui.not_checked_check_chatgpt_access_sends_a_request': (
        'Not checked. Check ChatGPT Access sends a request and uses your Codex allowance.'
    ),
    'tui.not_performed_setup_check': 'Not performed (setup check).',
    'tui.not_reported_yet': 'Not reported yet',
    'tui.not_verified': 'Not verified',
    'tui.outcome': 'Outcome',
    'tui.preparation_stopped': 'Preparation stopped.',
    'tui.preparing': 'Preparing {name}…',

    'tui.preparing.apply': 'Preparing {value}…',

    'tui.preparing_analysis': 'Preparing analysis…',
    'tui.project_description': 'Project Description: {project_description}',
    'tui.project_description_is_missing': 'Project description is missing.',
    'tui.project_description_not_set_add_project_description_to': (
        'Project Description: Not set. Add project_description to the config.'
    ),
    'tui.quit': 'Quit',
    'tui.refresh': 'Refresh',
    'tui.report': 'Report: {value}',
    'tui.report_text_recovered_some_checks_did_not_pass': 'Report text recovered, some checks did not pass.',
    'tui.reports': 'Reports: {reports_dir}',
    'tui.result': 'Result',

    'tui.result.show_step_details': 'Result: {result}',

    'tui.results': 'Results',

    'tui.results.select_config': 'Results: {status}{value}',

    'tui.results_directory': 'Results directory: {results_dir}',
    'tui.results_missing': ' (Missing)',
    'tui.review': 'Review: {value}',

    'tui.review.complete': 'Review',

    'tui.review_disabled_by_configuration': 'Review disabled by configuration.',
    'tui.run': 'Run',
    'tui.running_with_administrator_privileges': 'Running with administrator privileges.',
    'tui.see_diagnostics': 'See diagnostics.',
    'tui.see_limitations_and_diagnostics': 'See limitations and diagnostics.',
    'tui.select_a_configuration': 'Select a configuration.',
    'tui.select_a_folder_then_choose_use_folder': 'Select a folder, then choose Use folder.',
    'tui.source': 'Source',

    'tui.source.select_config': 'Source: {path}',
    'tui.source.show_step_details': 'Source: {value}',

    'tui.sources': 'sources',
    'tui.stage_agents_must_be_a_json_object': 'stage_agents.{stage} must be a JSON object.',
    'tui.status': 'Status',
    'tui.step': 'Step: {value}',
    'tui.step_revision': 'Step/revision',
    'tui.stopping_analysis_waiting_for_cleanup': 'Stopping analysis; waiting for cleanup…',
    'tui.technical_log': 'Technical log',
    'tui.the_audit_worker_exited_unexpectedly_inspect_any_saved': (
        'The audit worker exited unexpectedly; inspect any saved run files.'
    ),
    'tui.the_audit_worker_stopped_unexpectedly': 'The audit worker stopped unexpectedly.',
    'tui.the_configuration_has_no_codex_model_fields': 'The configuration has no Codex model fields.',
    'tui.the_interactive_interface_disconnected': 'The interactive interface disconnected.',
    'tui.the_saved_config_dir_must_be_an_absolute': 'The saved config_dir must be an absolute path.',
    'tui.the_stopped_before_this_step_completed': 'The run stopped before this step completed.',
    'tui.tokens': 'Tokens',
    'tui.unavailable': 'Unavailable',
    'tui.up': 'Up',
    'tui.use': 'Use',
    'tui.use_folder': 'Use folder',
    'tui.use_for': 'Use for',
    'tui.writing_the_architecture_report': 'Writing the architecture report.',
}


def message(key: str, **params) -> str:
    """Format one message; missing keys or parameters raise KeyError."""
    return MESSAGES[key].format(**params)


STAGE_MESSAGES = {
    'study': (message('run.analyzing_project'), message('run.project_analysis'),
              message('run.architecture_report_created')),
    'revise': (message('run.revising_architecture_report'), message('run.architecture_revision'),
               message('run.revised_architecture_report_created')),
    'review': (message('run.reviewing_report'), message('run.report_review'),
               message('run.review_response_saved_see_acceptance_and_findings')),
    'compare': (message('run.comparing_branch_reports'), message('run.branch_report_comparison'),
                message('run.branch_report_comparison_ready')),
}
