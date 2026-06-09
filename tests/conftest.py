"""rawos test configuration."""
import os
os.environ["RAWOS_TESTING"] = "1"
os.environ.setdefault("DEEPSEEK_KEY", "test-key")
os.environ["RAWOS_SANDBOX_DOCKER"] = "false"
os.environ.setdefault("JWT_SECRET", "test-secret-long-enough-for-production-use")
