// Rebuild a verified excerpt in JavaScript from the offsets the parent reports.
//
// The parent works in Python, whose string indices count **Unicode code points**. JavaScript's
// `String.prototype.slice` counts **UTF-16 code units**: every character outside the Basic Multilingual
// Plane (emoji, CJK extension B and beyond) occupies two units there but one code point in Python. Slicing
// the raw offsets therefore misaligns as soon as such a character appears before the excerpt, so convert
// first and slice second.
//
//   import { excerptFromOffsets, codePointToUtf16Index } from './excerpt-offsets.mjs';
//   const text = ...;                       // exactly the parent's normalised text
//   excerptFromOffsets(text, evidence);     // verifies against evidence.excerpt and throws on mismatch
//
// The text must be the same normalisation the parent used: UTF-8 decoded (BOM stripped) with CRLF folded
// to LF. A consumer that only displays `excerpt` does not need any of this.

/** UTF-16 index of the code point at `codePointOffset` (clamped to the string's length). */
export function codePointToUtf16Index(text, codePointOffset) {
  if (!Number.isInteger(codePointOffset) || codePointOffset < 0) {
    throw new Error('code point offset must be a non-negative integer: ' + codePointOffset);
  }
  let codePoints = 0;
  let index = 0;
  while (index < text.length && codePoints < codePointOffset) {
    const width = text.codePointAt(index) > 0xffff ? 2 : 1;
    index += width;
    codePoints += 1;
  }
  if (codePoints < codePointOffset) {
    throw new Error('code point offset ' + codePointOffset + ' is past the end of the text');
  }
  return index;
}

/** Code point index of the UTF-16 index `utf16Index`. */
export function utf16ToCodePointIndex(text, utf16Index) {
  let codePoints = 0;
  let index = 0;
  while (index < utf16Index && index < text.length) {
    index += text.codePointAt(index) > 0xffff ? 2 : 1;
    codePoints += 1;
  }
  return codePoints;
}

/**
 * Rebuild `item.excerpt` from `item.startChar` / `item.endChar`, or throw when it does not match.
 * Pass `expect` to check a known excerpt (the parent always sends one).
 */
export function excerptFromOffsets(text, item) {
  const start = codePointToUtf16Index(text, item.startChar);
  const end = codePointToUtf16Index(text, item.endChar);
  const rebuilt = text.slice(start, end);
  if (typeof item.excerpt === 'string' && rebuilt !== item.excerpt) {
    throw new Error('rebuilt excerpt does not match the one reported');
  }
  return rebuilt;
}
