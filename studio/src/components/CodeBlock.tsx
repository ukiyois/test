/* 代码块：语言标签 + 高亮 + 复制 + 插入输入框 */

import { memo, useMemo, useState } from "react";
import hljs from "highlight.js/lib/common";
import { toast } from "../stores/ui";

interface CodeBlockProps {
  code: string;
  lang: string;
  onInsert?: (code: string) => void;
}

const LANG_ALIASES: Record<string, string> = {
  js: "javascript",
  ts: "typescript",
  py: "python",
  sh: "bash",
  shell: "bash",
  yml: "yaml",
  md: "markdown",
  "c++": "cpp",
  "c#": "csharp",
};

export const CodeBlock = memo(function CodeBlock({ code, lang, onInsert }: CodeBlockProps) {
  const [copied, setCopied] = useState(false);
  const normalized = LANG_ALIASES[lang.toLowerCase()] ?? lang.toLowerCase();

  const html = useMemo(() => {
    try {
      if (normalized && hljs.getLanguage(normalized)) {
        return hljs.highlight(code, { language: normalized }).value;
      }
      return hljs.highlightAuto(code).value;
    } catch {
      return null;
    }
  }, [code, normalized]);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      setTimeout(() => setCopied(false), 1200);
    } catch {
      toast.error("复制失败");
    }
  };

  return (
    <div className="codeblock">
      <div className="codeblock__bar">
        <span className="codeblock__lang mono">{lang || "text"}</span>
        <span className="codeblock__actions">
          {onInsert && (
            <button className="btn btn--ghost btn--sm" onClick={() => onInsert(code)}>
              插入输入框
            </button>
          )}
          <button className="btn btn--ghost btn--sm" onClick={copy}>
            {copied ? "✓ 已复制" : "复制"}
          </button>
        </span>
      </div>
      {html != null ? (
        <pre>
          <code
            className={`hljs language-${normalized || "text"}`}
            dangerouslySetInnerHTML={{ __html: html }}
          />
        </pre>
      ) : (
        <pre>
          <code>{code}</code>
        </pre>
      )}
    </div>
  );
});
