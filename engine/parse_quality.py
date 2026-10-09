"""ParseQuality v1: safe extraction observations, never content/support verification."""
MAX_COUNT = 9007199254740991
MAX_PAGES = 256
MAX_PAGE = 100000

def normalize(value):
    if value is None:
        return None, False
    if isinstance(value, str):
        return (value.strip() or None, False) if len(value) <= 256 else (None, True)
    if not isinstance(value, dict):
        return None, True
    clean = {}
    for key in ("privateUseChars", "replacementChars", "searchableCjkRuns"):
        if key in value:
            n = value[key]
            if n is not None and (type(n) is not int or n < 0 or n > MAX_COUNT):
                return None, True
            clean[key] = n
    if "reviewPages" in value:
        pages = value["reviewPages"]
        if pages is None:
            clean["reviewPages"] = None
        elif not isinstance(pages, list) or len(pages) > MAX_PAGES or any(type(n) is not int or n < 1 or n > MAX_PAGE for n in pages):
            return None, True
        else:
            clean["reviewPages"] = sorted(set(pages))
    return clean, False
