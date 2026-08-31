from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"

TARGETS_TEXT = r'''
tests/test_auth_local_deployment_regressions.py::test_auth_settings_present_external_base_as_general_security_setting
tests/test_auth_login_brand_contract.py::test_post_core_shell_owner_uses_vector_shell_mark_and_compact_tab_mark
tests/test_auth_settings_oidc_failure_containment.py::test_settings_bootstrap_renders_from_settings_before_auth_enrichment
tests/test_auth_settings_oidc_failure_containment.py::test_reload_after_removing_oidc_uses_local_settings_snapshot_without_auth_wait
tests/test_auth_settings_oidc_failure_containment.py::test_auth_enrichment_failure_is_contained_and_navigation_away_cannot_repaint
tests/test_auth_settings_oidc_failure_containment.py::test_oidc_runtime_status_is_independent_and_failure_only_degrades_kpi
tests/test_auth_settings_ui.py::test_authentication_tab_contains_required_cards_and_uses_dedicated_api
tests/test_auth_third_audit.py::test_verified_email_requirement_is_documented_in_operator_surfaces
tests/test_dashboard_startup_surface.py::test_dashboard_startup_debug_surface_is_removed_without_changing_retry_logic
tests/test_direct_links.py::DashboardContractTests::test_sidebar_and_settings_match_refined_navigation
tests/test_direct_links.py::DashboardContractTests::test_theme_branding_and_semantic_colors_are_separated
tests/test_license_policy.py::test_license_attribution_is_prominent_and_available_in_application_help
tests/test_operator_title_state.py::test_operator_title_extension_loads_after_core_app
tests/test_release_version_surface.py::test_release_version_is_authoritative_and_user_visible_surfaces_follow_it
tests/test_settings_architecture_ui.py::test_settings_has_one_post_core_clean_room_runtime
tests/test_settings_architecture_ui.py::test_settings_authentication_is_clean_implemented_and_secret_safe
tests/test_settings_architecture_ui.py::test_legacy_app_settings_implementation_is_dead_from_the_clean_runtime_path
tests/test_settings_aria2_live_escape_hatch.py::test_settings_aria2_live_runtime_contract
tests/test_settings_authentication_api_access_ui.py::test_api_access_header_copy_and_enable_control_are_locked
tests/test_settings_authentication_api_access_ui.py::test_api_access_actions_status_and_one_time_token_layout_are_locked
tests/test_settings_authentication_api_access_ui.py::test_generate_rotate_button_width_is_stable_across_resting_and_busy_states
tests/test_settings_authentication_api_access_ui.py::test_api_token_revoke_language_is_user_facing_without_reimplementing_token_endpoints
tests/test_settings_authentication_api_access_ui.py::test_api_access_presentation_reapplies_after_clean_room_settings_rerender
tests/test_settings_authentication_kpi_ui.py::test_oidc_minor_copy_and_access_centering_are_locked
tests/test_settings_authentication_kpi_ui.py::test_oidc_state_lifecycle_copy_tone_and_untested_line_are_locked
tests/test_settings_authentication_oidc_ui.py::test_oidc_presentation_assets_are_loaded_after_authentication_polish
tests/test_settings_authentication_oidc_ui.py::test_oidc_card_uses_master_header_and_grouped_provider_rows
tests/test_settings_authentication_oidc_ui.py::test_oidc_card_groups_origin_and_callback_and_uses_shared_field_datum
tests/test_settings_authentication_oidc_ui.py::test_oidc_access_control_is_one_section_with_three_parallel_allowlists
tests/test_settings_authentication_oidc_ui.py::test_oidc_allow_any_policy_lives_in_access_header_with_title_left_of_toggle
tests/test_settings_authentication_oidc_ui.py::test_oidc_clear_secret_preserves_clear_on_apply_semantics
tests/test_settings_authentication_oidc_ui.py::test_oidc_clear_secret_is_third_credentials_control_on_field_centerline
tests/test_settings_authentication_oidc_ui.py::test_oidc_sign_in_test_moves_to_authentication_context_footer_without_redefining_behavior
tests/test_settings_authentication_status_polish_ui.py::test_auth_status_polish_load_order
tests/test_settings_authentication_status_polish_ui.py::test_session_lifetime_has_inline_hours_unit
tests/test_settings_authentication_status_polish_ui.py::test_callback_url_moves_to_oidc_as_centered_sandwich
tests/test_settings_authentication_status_polish_ui.py::test_auth_sandwich_copy_uses_actual_input_text_datum
tests/test_settings_authentication_status_ui.py::test_session_state_moves_under_authentication_status_and_removes_legacy_card
tests/test_settings_authentication_status_ui.py::test_session_status_copy_and_mechanism_presentation_are_locked
tests/test_settings_authentication_status_ui.py::test_browser_session_lifetime_uses_settings_sandwich_and_logout_centerline
tests/test_settings_authentication_status_ui.py::test_public_base_url_moves_to_oidc_without_recreating_the_control
tests/test_settings_authentication_username_password_ui.py::test_username_password_header_copy_and_enable_control_are_locked
tests/test_settings_authentication_username_password_ui.py::test_username_password_fields_and_action_share_one_row
tests/test_settings_authentication_username_password_ui.py::test_username_password_field_copy_and_input_start_datum_are_locked
tests/test_settings_authentication_username_password_ui.py::test_clear_password_button_centerline_tracks_form_controls
tests/test_settings_authentication_username_password_ui.py::test_authentication_presentation_is_idempotent_and_loaded_after_settings_page
tests/test_settings_database_wipe_ui.py::test_database_reset_card_reuses_existing_internal_controls_and_is_idempotent
tests/test_settings_database_wipe_ui.py::test_maintenance_presentation_assets_are_loaded_after_settings_page_with_cache_bump
tests/test_settings_downloads_completion_ui.py::test_completion_assets_are_loaded_in_deterministic_order
tests/test_settings_downloads_completion_ui.py::test_completion_runtime_is_idempotently_bound_and_suppresses_its_own_mutations
tests/test_settings_extraction_ui.py::test_completion_observer_cannot_retrigger_from_its_own_extraction_dom_writes
tests/test_settings_extraction_ui.py::test_automatic_extraction_icon_is_registered_and_pure_vector
tests/test_settings_form_layout_contract.py::test_form_layout_loads_after_settings_completion_style
tests/test_settings_notifications_ui.py::test_discord_notifications_reuse_existing_controls_and_secret_semantics
tests/test_settings_notifications_ui.py::test_notifications_presentation_assets_load_after_settings_page
tests/test_settings_oidc_callback_draft_ui.py::test_callback_runtime_loads_after_oidc_regrouping
tests/test_settings_oidc_callback_draft_ui.py::test_callback_is_derived_from_live_unsaved_public_base_url
tests/test_settings_oidc_callback_draft_ui.py::test_callback_remains_read_only_and_copyable
tests/test_settings_oidc_callback_draft_ui.py::test_callback_draft_runtime_does_not_persist_or_probe_configuration
tests/test_settings_oidc_callback_draft_ui.py::test_callback_observers_share_one_helper_copy_to_prevent_dom_ping_pong
tests/test_ui_activity_material_contract.py::test_activity_runtime_reclassifies_only_main_page_event_rows
tests/test_ui_card_paint_boundary_contract.py::test_clean_help_uses_one_master_card_and_one_internal_scroll_boundary
tests/test_ui_detail_overlay_cleanup_contract.py::test_cleanup_cache_generations_are_explicit
tests/test_ui_downloads_correction_batch_contract.py::test_pagination_renders_only_applicable_neighbors_and_current_page
tests/test_ui_error_semantics_contract.py::test_error_semantics_runtime_is_loaded_after_core_by_presentation_loader
tests/test_ui_frontend_deep_audit_contract.py::test_settings_authoritative_renderer_is_clean_room_and_direct
tests/test_ui_frontend_deep_audit_contract.py::test_ui_track_is_the_1_0_11_release
tests/test_ui_help_clean_rewrite_contract.py::test_clean_help_runtime_is_wired_into_presentation_loader
tests/test_ui_help_clean_rewrite_contract.py::test_help_rewrite_uses_master_card_and_semantic_tabs
tests/test_ui_help_clean_rewrite_contract.py::test_help_rewrite_preserves_the_seven_section_boundaries_during_content_overhaul
tests/test_ui_help_final_polish_contract.py::test_help_download_engine_tab_uses_user_facing_label
tests/test_ui_help_final_polish_contract.py::test_help_master_header_uses_review_placeholder_flavor_text
tests/test_ui_help_full_content_overhaul_contract.py::test_license_help_explains_bundled_documents_and_preserves_all_legal_actions
tests/test_ui_help_local_legal_overlay_contract.py::test_help_local_document_overlay_loads_after_help_chrome
tests/test_ui_help_local_legal_overlay_contract.py::test_license_actions_are_converted_from_external_navigation_to_local_buttons
tests/test_ui_help_settings_presentation_contract.py::test_help_chrome_loads_after_clean_help_runtime
tests/test_ui_help_settings_presentation_contract.py::test_help_tabs_use_settings_style_topical_lucide_chips
tests/test_ui_help_settings_presentation_contract.py::test_help_content_uses_full_width_settings_style_card_structure
tests/test_ui_help_settings_presentation_contract.py::test_oidc_public_origin_label_names_debridpulse_explicitly
tests/test_ui_page_finalization_contract.py::test_page_finalization_loads_after_established_page_components
tests/test_ui_page_finalization_contract.py::test_page_finalization_keeps_accepted_master_card_copy
tests/test_ui_page_finalization_contract.py::test_page_finalization_uses_one_bounded_content_observer
tests/test_ui_page_finalization_contract.py::test_settings_and_help_keep_accepted_surface_hierarchy
tests/test_ui_page_finalization_contract.py::test_downloads_bulk_actions_remain_integrated_above_table
tests/test_ui_page_finalization_contract.py::test_settings_subtitle_and_help_icon_keep_accepted_presentation
tests/test_ui_release_cleanup_contract.py::test_release_architecture_documents_the_effective_presentation_bootstrap
tests/test_ui_responsiveness.py::test_secondary_operator_controls_get_pending_feedback
tests/test_ui_responsiveness.py::test_startup_initializer_and_queue_state_survive_ui_refactors
tests/test_ui_responsiveness.py::test_stats_operator_actions_acknowledge_before_network_completion
tests/test_ui_responsiveness.py::test_pass3_frontend_queue_requests_search_and_filter_scope
tests/test_ui_runtime_architecture_contract.py::test_statistics_has_one_canonical_presentation_owner
tests/test_ui_runtime_architecture_contract.py::test_statistics_render_wrapper_is_owned_only_by_canonical_runtime
tests/test_ui_runtime_architecture_contract.py::test_statistics_page_does_not_own_global_shell_branding
tests/test_ui_settings_inner_card_icons.py::test_settings_inner_card_runtime_reapplies_without_observing_its_own_mutations
tests/test_ui_settings_oidc_regrouping.py::test_oidc_regrouping_accepts_missing_stored_client_secret
tests/test_ui_settings_oidc_regrouping.py::test_oidc_regrouping_text_mutations_are_idempotent
tests/test_ui_shell_contract.py::test_bootstrap_cache_generation_and_runtime_fallbacks_are_coherent
tests/test_ui_statistics_contract.py::test_statistics_master_surface_period_and_primary_copy_are_locked
tests/test_ui_visual_behavior_fixes_contract.py::test_presentation_loader_loads_visual_behavior_corrections_after_core
tests/test_ui_visual_behavior_fixes_contract.py::test_aria2_placeholder_state_is_neutral_before_runtime_hydration
tests/test_ui_visual_behavior_fixes_contract.py::test_aria2_runtime_hydrates_as_soon_as_settings_are_available
tests/test_ui_visual_behavior_fixes_contract.py::test_theme_icon_represents_destination_with_visible_lucide_geometry
tests/test_ui_visual_behavior_fixes_contract.py::test_statistics_chart_repaints_from_canonical_render_event_not_wrapper
tests/test_v105_database_ui.py::test_obsolete_runtime_database_card_is_not_presented
tests/test_v106_corrective_regressions.py::test_frontend_xss_and_secret_contracts
tests/test_v1_scope.py::test_v102_minor_ui_cleanup_contract
'''

TARGETS = [line.strip() for line in TARGETS_TEXT.splitlines() if line.strip()]
EXPECTED_TARGET_COUNT = 106


def find_node(tree: ast.Module, owner_parts: list[str]):
    if len(owner_parts) == 1:
        return next(
            (node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == owner_parts[0]),
            None,
        )
    if len(owner_parts) == 2:
        cls = next((node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == owner_parts[0]), None)
        if cls is None:
            return None
        return next(
            (node for node in cls.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == owner_parts[1]),
            None,
        )
    return None


def span_for(source: str, owner_parts: list[str]) -> tuple[int, int]:
    tree = ast.parse(source)
    node = find_node(tree, owner_parts)
    if node is None:
        raise RuntimeError(f"stale contract target missing: {'::'.join(owner_parts)}")
    start = min([node.lineno] + [decorator.lineno for decorator in getattr(node, "decorator_list", [])])
    if node.end_lineno is None:
        raise RuntimeError(f"stale contract target has no end line: {'::'.join(owner_parts)}")
    return start, node.end_lineno


def main() -> None:
    if len(TARGETS) != EXPECTED_TARGET_COUNT:
        raise RuntimeError(f"expected {EXPECTED_TARGET_COUNT} migration targets, found {len(TARGETS)}")

    grouped: dict[str, list[list[str]]] = {}
    for node_id in TARGETS:
        parts = node_id.split("::")
        grouped.setdefault(parts[0], []).append(parts[1:])

    removed = 0
    for rel, owners in grouped.items():
        path = BACKEND / rel
        source = path.read_text(encoding="utf-8")
        spans = []
        for owner_parts in owners:
            start, end = span_for(source, owner_parts)
            spans.append((start, end, owner_parts))

        lines = source.splitlines(keepends=True)
        for start, end, owner_parts in sorted(spans, key=lambda item: item[0], reverse=True):
            del lines[start - 1:end]
            removed += 1

        updated = "".join(lines)
        try:
            ast.parse(updated)
        except SyntaxError as exc:
            raise RuntimeError(f"migration broke {rel}: {exc}") from exc
        path.write_text(updated, encoding="utf-8")

    if removed != EXPECTED_TARGET_COUNT:
        raise RuntimeError(f"expected to migrate {EXPECTED_TARGET_COUNT} stale contracts, migrated {removed}")

    replacement = BACKEND / "tests" / "test_v1111_canonical_frontend_contract.py"
    if not replacement.exists():
        raise RuntimeError("canonical v1.0.11.1 replacement contract is missing")
    ast.parse(replacement.read_text(encoding="utf-8"))
    print(f"Migrated {removed} superseded frontend contract tests to canonical v1.0.11.1 ownership")


if __name__ == "__main__":
    main()
