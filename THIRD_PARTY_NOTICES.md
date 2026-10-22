# Third-party notices

llm-relay is released under the MIT License (see `LICENSE`). The components
below were adapted from other open-source projects and are redistributed under
their original terms, reproduced here in full as those terms require.

## claude-code-cache-fix

- Upstream: https://github.com/cnighswonger/claude-code-cache-fix (v1.7.2)
- Used in: `src/llm_relay/proxy/cc_cache_fix.py` (request normalisation for
  stable prompt caching: tool ordering and `cache_control` TTL handling)
- License: MIT

```
MIT License

Copyright (c) 2026 Chris Nighswonger, Victor Sun (@VictorSun92), @jmarianski

If you contributed to this work and are not listed above, please open an issue
or pull request — we want to credit everyone properly.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
