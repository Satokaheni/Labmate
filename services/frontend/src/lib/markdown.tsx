import { Fragment, memo, type ReactNode } from 'react';

function renderInline(text: string, keyPrefix: string): ReactNode[] {
  const out: ReactNode[] = [];
  const codeParts = text.split(/(`[^`]+`)/g);
  codeParts.forEach((part, ci) => {
    if (part.startsWith('`') && part.endsWith('`') && part.length >= 2) {
      out.push(
        <code key={`${keyPrefix}-c${ci}`} className="rounded bg-page px-1 py-0.5 font-mono text-[0.85em]">
          {part.slice(1, -1)}
        </code>
      );
      return;
    }
    const boldParts = part.split(/(\*\*[^*]+\*\*)/g);
    boldParts.forEach((bp, bi) => {
      if (bp.startsWith('**') && bp.endsWith('**') && bp.length >= 4) {
        out.push(<strong key={`${keyPrefix}-b${ci}-${bi}`}>{bp.slice(2, -2)}</strong>);
      } else if (bp) {
        out.push(<Fragment key={`${keyPrefix}-t${ci}-${bi}`}>{bp}</Fragment>);
      }
    });
  });
  return out;
}

export const Markdown = memo(function Markdown({ text }: { text: string }) {
  const blocks = text.split(/```/g);
  return (
    <div className="whitespace-pre-wrap break-words text-sm leading-relaxed text-primary-alt">
      {blocks.map((block, i) => {
        if (i % 2 === 1) {
          const body = block.replace(/^[a-zA-Z]*\n/, '');
          return (
            <pre
              key={`code-${i}`}
              className="my-2 overflow-x-auto rounded-pill border border-border-2 bg-page p-3 font-mono text-xs"
            >
              <code>{body.replace(/\n$/, '')}</code>
            </pre>
          );
        }
        return (
          <Fragment key={`prose-${i}`}>
            {block.split('\n').map((line, li) => (
              <Fragment key={`prose-${i}-${li}`}>
                {li > 0 && <br />}
                {renderInline(line, `p${i}l${li}`)}
              </Fragment>
            ))}
          </Fragment>
        );
      })}
    </div>
  );
});
