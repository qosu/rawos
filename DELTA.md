CHANGED: rawos/kernel/arch/base.py + rawos/kernel/arch/linux.py (ServiceManager Protocol + LinuxServiceManager generate/install/uninstall_unit)
CHANGED: rawos/kernel/arch/linux.py + rawos/installer/setup.py (SetupWizard)
CHANGED: rawos/cli/main.py (+rawos service CLI group, +rawos setup command)
CHANGED: tests/test_arch_linux_service_manager.py (25 tests), test_cli_service.py (10), test_setup_wizard.py (12), test_tier1_remaining_prefixes.py (3, from rawos self-probe)
CHANGED: pyproject.toml (+python-telegram-bot>=22.8, +openai>=2.41)
WHY: Milestone 5 (Installable substrate) — rawos is now self-installable on any Linux host via `rawos setup` + `rawos service install`. 685 tests pass. Repo cleaned: 252 self-improve branches pruned (4 archived), 67 leaked worktrees removed.
NEXT: Phase 0 consolidation complete. Next: Phase 1 Step A cage hardening (escape-vector tests: symlink, rename-into-TIER0, path traversal, absolute-path, hardlink).
