import { CHARACTER_LIMIT } from '../constants.js';
export function truncate(text, offset = 0, limit = CHARACTER_LIMIT) {
    const slice = text.slice(offset, offset + limit);
    const has_more = offset + limit < text.length;
    return {
        text: slice + (has_more
            ? `\n\n[TRUNCATED: showing chars ${offset}–${offset + slice.length} of ${text.length} total. ` +
                `Call again with offset=${offset + limit} to continue.]`
            : ''),
        has_more,
        next_offset: has_more ? offset + limit : null,
        total: text.length,
    };
}
//# sourceMappingURL=truncate.js.map