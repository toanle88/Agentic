import unittest

import tests
from agentic_runner import cli as agentic
from agentic_runner import config as agentic_config


class ConfigModuleTests(unittest.TestCase):
    def test_runner_reexports_configuration_boundary(self):
        self.assertIs(agentic.load_config, agentic_config.load_config)
        self.assertIs(agentic.preflight_agent_settings, agentic_config.preflight_agent_settings)
        self.assertIs(agentic.detect_test_commands, agentic_config.detect_test_commands)
        self.assertIs(agentic.default_provider_settings, agentic_config.default_provider_settings)

    def test_configuration_constants_are_shared_with_runner(self):
        self.assertEqual(agentic.SUPPORTED_PROVIDERS, agentic_config.SUPPORTED_PROVIDERS)
        self.assertEqual(agentic.SECURITY_MODES, agentic_config.SECURITY_MODES)
        self.assertEqual(
            agentic.DEFAULT_OPENROUTER_BASE_URL,
            agentic_config.DEFAULT_OPENROUTER_BASE_URL,
        )
        for name in (
            "DEFAULT_SECURITY_MODE",
            "PROVIDER_SANDBOX_ENV",
            "OPENROUTER_MAX_TOOL_TURNS",
            "OPENROUTER_COMMAND_TIMEOUT",
            "DEFAULT_DEVELOPER_TIMEOUT",
            "MAX_TASK_TIMEOUT",
        ):
            self.assertIs(getattr(agentic, name), getattr(agentic_config, name))


if __name__ == "__main__":
    unittest.main()
