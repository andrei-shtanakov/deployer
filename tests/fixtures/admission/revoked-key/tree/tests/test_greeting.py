"""Project's own test: the greeting text is part of the contract."""

import unittest

from ci_build import GREETING


class GreetingTest(unittest.TestCase):
    def test_greeting_text(self) -> None:
        self.assertEqual(GREETING, "hello from ci-build", "greeting text drifted")


if __name__ == "__main__":
    unittest.main()
