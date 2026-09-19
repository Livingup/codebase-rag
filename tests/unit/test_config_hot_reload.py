import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from codebase_rag.common.keypool import KeyPool
from codebase_rag.config.loader import ConfigManager, load_config, parse_env_file
from codebase_rag.config.schema import AppConfig
from codebase_rag.services.agent.service import AgentService
from codebase_rag.services.management_api.models import ReloadConfigResponse


def test_parse_env_file(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\n"
        "FOO=bar\n"
        "QUOTED=\"hello world\"\n"
        "SINGLE='single quoted'\n"
        "export EXPORTED=value\n",
        encoding="utf-8",
    )
    parsed = parse_env_file(env_file)
    assert parsed["FOO"] == "bar"
    assert parsed["QUOTED"] == "hello world"
    assert parsed["SINGLE"] == "single quoted"
    assert parsed["EXPORTED"] == "value"


@pytest.mark.asyncio
async def test_keypool_update_keys_preserves_telemetry():
    kp = KeyPool(keys=["key-1", "key-2"], cooldown_seconds=60, max_consecutive_failures=3)
    # Simulate failures on key-1
    await kp.report_failure("key-1")
    await kp.report_failure("key-1")
    assert kp._states[0].consecutive_failures == 2

    # Update keys: key-1 kept, key-2 removed, key-3 added
    await kp.update_keys(keys=["key-1", "key-3"], cooldown_seconds=120)

    assert kp.total_keys == 2
    # key-1 preserves consecutive_failures
    assert kp._states[0].key == "key-1"
    assert kp._states[0].consecutive_failures == 2
    # key-3 is fresh
    assert kp._states[1].key == "key-3"
    assert kp._states[1].consecutive_failures == 0
    assert kp._cooldown_seconds == 120


@pytest.mark.asyncio
async def test_config_manager_reconciliation(tmp_path: Path):
    config_data = {
        "management_api": {"server": {"host": "127.0.0.1", "port": 18080}},
        "mcp": {
            "reranker": {
                "providers": [
                    {"provider": "deepseek", "model": "deepseek-flash", "priority": 10}
                ]
            },
        },
    }
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml.dump(config_data), encoding="utf-8")

    initial_cfg = load_config(cfg_file)
    cm = ConfigManager(initial_cfg, cfg_file)

    reconciled_called = False

    async def mock_reconcile(old_cfg: AppConfig, new_cfg: AppConfig) -> None:
        nonlocal reconciled_called
        reconciled_called = True
        assert old_cfg.mcp.reranker.providers[0].priority == 10
        assert new_cfg.mcp.reranker.providers[0].priority == 5

    cm.register_reconciler("test_service", mock_reconcile)

    # Modify config (change priority from 10 to 5)
    config_data["mcp"]["reranker"]["providers"][0]["priority"] = 5
    cfg_file.write_text(yaml.dump(config_data), encoding="utf-8")

    new_cfg = await cm.reload()
    assert reconciled_called is True
    assert new_cfg.mcp.reranker.providers[0].priority == 5
    assert "test_service" in cm.last_reconciled_services
    assert cm.last_restart_reasons == []


@pytest.mark.asyncio
async def test_config_manager_detects_immutable_port_change(tmp_path: Path):
    config_data = {
        "management_api": {"server": {"host": "127.0.0.1", "port": 18080}},
    }
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml.dump(config_data), encoding="utf-8")

    initial_cfg = load_config(cfg_file)
    cm = ConfigManager(initial_cfg, cfg_file)

    # Change management_api.server.port
    config_data["management_api"]["server"]["port"] = 18088
    cfg_file.write_text(yaml.dump(config_data), encoding="utf-8")

    await cm.reload()
    assert len(cm.last_restart_reasons) == 1
    assert "Management API port changed" in cm.last_restart_reasons[0]


@pytest.mark.asyncio
async def test_agent_service_reconcile():
    cm = MagicMock()
    cm.config = MagicMock()

    service = AgentService(config_manager=cm)

    old_cfg = MagicMock()
    old_pcfg = MagicMock()
    old_pcfg.api_format = "openai"
    old_pcfg.base_url = "https://api.openai.com/v1"
    old_pcfg.default_model = "gpt-4"
    old_pcfg.api_keys = [MagicMock(key="key-old")]
    old_pcfg.max_concurrency = 5
    old_pcfg.retry.key_cooldown_seconds = 60
    old_pcfg.retry.max_retries_per_key = 3
    old_cfg.agent.llm.providers = {"primary": old_pcfg}

    new_cfg = MagicMock()
    new_pcfg = MagicMock()
    new_pcfg.api_format = "openai"
    new_pcfg.base_url = "https://api.openai.com/v1"
    new_pcfg.default_model = "gpt-4"
    new_pcfg.api_keys = [MagicMock(key="key-old"), MagicMock(key="key-new")]
    new_pcfg.max_concurrency = 10
    new_pcfg.retry.key_cooldown_seconds = 120
    new_pcfg.retry.max_retries_per_key = 5

    # Also add a new secondary provider
    new_sec = MagicMock()
    new_sec.api_format = "openai"
    new_sec.base_url = "https://api.deepseek.com/v1"
    new_sec.default_model = "deepseek-chat"
    new_sec.api_keys = [MagicMock(key="sk-deepseek")]
    new_sec.max_concurrency = 8
    new_sec.retry.key_cooldown_seconds = 30
    new_sec.retry.max_retries_per_key = 3

    new_cfg.agent.llm.providers = {
        "primary": new_pcfg,
        "secondary": new_sec,
    }

    # Initialize with old provider
    with patch("codebase_rag.services.agent.service._create_provider") as mock_create:
        mock_create.return_value = MagicMock()
        cm.config.agent = old_cfg.agent
        await service.initialize()
        assert "primary" in service._providers
        assert service._keypools["primary"].total_keys == 1

        # Reconcile with new config
        await service.reconcile(old_cfg, new_cfg)
        assert "primary" in service._providers
        assert "secondary" in service._providers
        assert service._keypools["primary"].total_keys == 2
        assert service._keypools["secondary"].total_keys == 1
        assert service._provider_sems["primary"]._value == 10
