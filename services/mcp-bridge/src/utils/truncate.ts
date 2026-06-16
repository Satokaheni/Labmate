import { CHARACTER_LIMIT } from '../constants.js';

export interface TruncateResult {
  text:        string;
  has_more:    boolean;
  next_offset: number | null;
  total:       number;
}

export function truncate(
  text:   string,
  offset: number = 0,
  limit:  number = CHARACTER_LIMIT,
): TruncateResult {
  const slice    = text.slice(offset, offset + limit);
  const has_more = offset + limit < text.length;
  return {
    text: slice + (has_more
      ? `\n\n[TRUNCATED: showing chars ${offset}–${offset + slice.length} of ${text.length} total. ` +
        `Call again with offset=${offset + limit} to continue.]`
      : ''),
    has_more,
    next_offset: has_more ? offset + limit : null,
    total:       text.length,
  };
}
