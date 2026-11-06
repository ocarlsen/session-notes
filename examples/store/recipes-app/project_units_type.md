---
name: Units are a type, not a string
description: quantities carry a Unit enum, parsed once
metadata:
  type: project
---
Every quantity is `(Decimal, Unit)`. The parser is the only place that turns
text into a Unit; everything downstream compares enums.

**Why:** two string parsers disagreed on "T" vs "t" (tablespoon vs teaspoon).
