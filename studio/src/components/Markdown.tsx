/* Markdown 渲染：marked 解析 + 自管 DOM 渲染，代码块带高亮/语言标签/复制。
   不直接 dangerouslySetInnerHTML 全文，代码块用 React 组件渲染以便挂按钮。 */

import { memo, useMemo } from "react";
import { marked, type Token, type Tokens } from "marked";
import hljs from "highlight.js/lib/common";
import { CodeBlock } from "./CodeBlock";

marked.setOptions({ gfm: true, breaks: true });

/* 行内 Markdown 的轻量渲染（粗体/斜体/行内码/链接/删除线） */
function renderInline(text: string, keyPrefix: string): React.ReactNode[] {
  const nodes: React.ReactNode[] = [];
  // 顺序：code > bold > italic > strike > link
  const pattern =
    /(`[^`\n]+`)|(\*\*[^*]+\*\*)|(\*[^*\n]+\*)|(~~[^~]+~~)|(\[[^\]]+\]\((?:https?:\/\/)[^)\s]+\))/g;
  let last = 0;
  let match: RegExpExecArray | null;
  let i = 0;
  while ((match = pattern.exec(text)) !== null) {
    if (match.index > last) {
      nodes.push(text.slice(last, match.index));
    }
    const [full, code, bold, italic, strike, link] = match;
    const key = `${keyPrefix}-${i++}`;
    if (code) {
      nodes.push(
        <code key={key} className="md-code">
          {code.slice(1, -1)}
        </code>,
      );
    } else if (bold) {
      nodes.push(<strong key={key}>{renderInline(bold.slice(2, -2), key)}</strong>);
    } else if (italic) {
      nodes.push(<em key={key}>{italic.slice(1, -1)}</em>);
    } else if (strike) {
      nodes.push(<del key={key}>{strike.slice(2, -2)}</del>);
    } else if (link) {
      const labelEnd = full.indexOf("](");
      const label = full.slice(1, labelEnd);
      const href = full.slice(labelEnd + 2, -1);
      nodes.push(
        <a key={key} href={href} target="_blank" rel="noreferrer">
          {label}
        </a>,
      );
    } else {
      nodes.push(full);
    }
    last = match.index + full.length;
  }
  if (last < text.length) nodes.push(text.slice(last));
  return nodes;
}

function renderToken(token: Token, key: string): React.ReactNode {
  switch (token.type) {
    case "heading": {
      const t = token as Tokens.Heading;
      const Tag = (`h${Math.min(t.depth, 4)}`) as "h1" | "h2" | "h3" | "h4";
      return <Tag key={key}>{renderInline(t.text, key)}</Tag>;
    }
    case "paragraph": {
      const t = token as Tokens.Paragraph;
      return <p key={key}>{renderInline(t.text, key)}</p>;
    }
    case "code": {
      const t = token as Tokens.Code;
      return <CodeBlock key={key} code={t.text} lang={t.lang || ""} />;
    }
    case "blockquote": {
      const t = token as Tokens.Blockquote;
      return (
        <blockquote key={key}>
          {t.tokens?.map((child, i) => renderToken(child, `${key}-${i}`)) ??
            renderInline(t.text, key)}
        </blockquote>
      );
    }
    case "list": {
      const t = token as Tokens.List;
      const Tag = t.ordered ? "ol" : "ul";
      return (
        <Tag key={key} start={t.ordered && typeof t.start === "number" ? t.start : undefined}>
          {t.items.map((item, i) => (
            <li key={`${key}-${i}`}>
              {item.tokens
                ? item.tokens.map((child, j) =>
                    child.type === "text"
                      ? renderInline((child as Tokens.Text).text, `${key}-${i}-${j}`)
                      : renderToken(child, `${key}-${i}-${j}`),
                  )
                : renderInline(item.text, `${key}-${i}`)}
            </li>
          ))}
        </Tag>
      );
    }
    case "hr":
      return <hr key={key} />;
    case "table": {
      const t = token as Tokens.Table;
      return (
        <div key={key} className="md-table-wrap">
          <table>
            <thead>
              <tr>
                {t.header.map((cell, i) => (
                  <th key={i}>{renderInline(cell.text, `${key}-h${i}`)}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {t.rows.map((row, r) => (
                <tr key={r}>
                  {row.map((cell, c) => (
                    <td key={c}>{renderInline(cell.text, `${key}-${r}-${c}`)}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      );
    }
    case "space":
      return null;
    case "html": {
      // 不信任原始 HTML，转义为文本
      const t = token as Tokens.HTML;
      return (
        <p key={key}>
          <code className="md-code">{t.text}</code>
        </p>
      );
    }
    default: {
      const t = token as Tokens.Text;
      if (typeof t.text === "string") {
        return <p key={key}>{renderInline(t.text, key)}</p>;
      }
      return null;
    }
  }
}

export const Markdown = memo(function Markdown({ text }: { text: string }) {
  const tokens = useMemo(() => marked.lexer(text), [text]);
  return (
    <div className="md">
      {tokens.map((token, i) => renderToken(token, `t${i}`))}
    </div>
  );
});

export { hljs };
