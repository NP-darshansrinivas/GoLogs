import React, { useState } from 'react';

interface ExportReportButtonProps {
  caseId: string;
}

interface ReportResponse {
  format: string;
  content: string;
}

function renderInlineFormatting(text: string): (string | JSX.Element)[] {
  const parts: (string | JSX.Element)[] = [];
  const regex = /(\*\*[^*]+\*\*|`[^`]+`|_[^_]+_)/g;
  let lastIndex = 0;
  let match: RegExpExecArray | null;

  while ((match = regex.exec(text)) !== null) {
    if (match.index > lastIndex) {
      parts.push(text.substring(lastIndex, match.index));
    }
    const m = match[0];
    if (m.startsWith("**") && m.endsWith("**")) {
      parts.push(
        <strong key={match.index} className="font-semibold text-text-primary">
          {m.slice(2, -2)}
        </strong>
      );
    } else if (m.startsWith("`") && m.endsWith("`")) {
      parts.push(
        <code key={match.index} className="rounded bg-bg-inset px-1.5 py-0.5 font-mono text-[11px] text-accent border border-border">
          {m.slice(1, -1)}
        </code>
      );
    } else if (m.startsWith("_") && m.endsWith("_")) {
      parts.push(
        <em key={match.index} className="italic text-text-muted">
          {m.slice(1, -1)}
        </em>
      );
    }
    lastIndex = regex.lastIndex;
  }

  if (lastIndex < text.length) {
    parts.push(text.substring(lastIndex));
  }

  return parts;
}

function FormattedReport({ content }: { content: string }): JSX.Element {
  const lines = content.split("\n");
  const elements: JSX.Element[] = [];
  let inTable = false;
  let tableHeader: string[] = [];
  let tableRows: string[][] = [];

  const flushTable = (key: string) => {
    if (tableHeader.length > 0 || tableRows.length > 0) {
      elements.push(
        <div key={key} className="my-4 overflow-x-auto rounded-lg border border-border bg-bg-inset">
          <table className="w-full text-left text-xs">
            {tableHeader.length > 0 && (
              <thead className="border-b border-border bg-bg-elevated text-text-primary font-semibold">
                <tr>
                  {tableHeader.map((h, i) => (
                    <th key={i} className="px-3 py-2 font-mono text-[11px] uppercase tracking-wider">{h.trim()}</th>
                  ))}
                </tr>
              </thead>
            )}
            <tbody className="divide-y divide-border text-text-secondary font-mono text-xs">
              {tableRows.map((row, rIdx) => (
                <tr key={rIdx} className="hover:bg-bg-surface/50">
                  {row.map((cell, cIdx) => (
                    <td key={cIdx} className="px-3 py-2 whitespace-nowrap">
                      {cell.trim().replace(/^`|`$/g, "")}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      );
      tableHeader = [];
      tableRows = [];
    }
  };

  lines.forEach((line, idx) => {
    const trimmed = line.trim();
    if (trimmed.startsWith("|")) {
      inTable = true;
      const cells = trimmed.split("|").slice(1, -1);
      if (cells.every((c) => c.trim().startsWith("-") || c.trim().startsWith(":-"))) {
        return;
      }
      if (tableHeader.length === 0) {
        tableHeader = cells;
      } else {
        tableRows.push(cells);
      }
      return;
    }

    if (inTable) {
      inTable = false;
      flushTable(`table-${idx}`);
    }

    if (!trimmed) {
      elements.push(<div key={idx} className="h-1.5" />);
      return;
    }

    if (trimmed.startsWith("# ")) {
      elements.push(
        <h1 key={idx} className="mt-3 mb-2 text-lg font-bold text-text-primary border-b border-border pb-1.5">
          {trimmed.replace("# ", "")}
        </h1>
      );
    } else if (trimmed.startsWith("## ")) {
      elements.push(
        <h2 key={idx} className="mt-4 mb-2 text-sm font-semibold text-text-primary border-b border-border pb-1">
          {trimmed.replace("## ", "")}
        </h2>
      );
    } else if (trimmed.startsWith("### ")) {
      elements.push(
        <h3 key={idx} className="mt-3 mb-1 text-xs font-semibold text-accent">
          {trimmed.replace("### ", "")}
        </h3>
      );
    } else if (trimmed.startsWith("- ")) {
      elements.push(
        <li key={idx} className="ml-4 list-disc text-xs text-text-secondary leading-relaxed">
          {renderInlineFormatting(trimmed.replace("- ", ""))}
        </li>
      );
    } else {
      elements.push(
        <p key={idx} className="text-xs text-text-secondary leading-relaxed">
          {renderInlineFormatting(trimmed)}
        </p>
      );
    }
  });

  if (inTable) {
    flushTable("table-end");
  }

  return <div className="space-y-1">{elements}</div>;
}

export const ExportReportButton: React.FC<ExportReportButtonProps> = ({ caseId }) => {
  const [isLoading, setIsLoading] = useState(false);
  const [reportModalContent, setReportModalContent] = useState<string | null>(null);

  const handleExport = async () => {
    if (!caseId || typeof caseId !== "string" || !caseId.trim()) {
      alert("No active case selected for export.");
      return;
    }
    setIsLoading(true);
    try {
      const response = await fetch(`/api/cases/${encodeURIComponent(caseId)}/report`, {
        method: 'POST',
      });

      if (!response.ok) {
        throw new Error(`Export failed with status: ${response.status}`);
      }

      const data: ReportResponse = await response.json();
      setReportModalContent(data.content);

      const blob = new Blob([data.content], { type: 'text/markdown' });
      const url = window.URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = `Investigation_Report_${caseId}.md`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.URL.revokeObjectURL(url);
    } catch (error) {
      console.error('Failed to export report:', error);
      alert('Failed to generate the report. Please ensure the backend is running and check the console.');
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <>
      <button
        onClick={handleExport}
        disabled={isLoading}
        className="px-4 py-2 bg-accent text-bg-primary text-sm font-semibold rounded shadow hover:opacity-90 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
      >
        {isLoading ? 'Generating...' : 'Export Report'}
      </button>

      {reportModalContent && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-bg-primary/85 backdrop-blur-sm p-4">
          <div className="flex flex-col max-h-[85vh] w-full max-w-3xl rounded-xl border border-border bg-bg-surface p-5 shadow-2xl">
            <div className="flex items-center justify-between border-b border-border pb-3 mb-3">
              <h3 className="text-sm font-semibold text-text-primary">Exported Investigation Report</h3>
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={() => {
                    navigator.clipboard.writeText(reportModalContent);
                    alert("Report copied to clipboard!");
                  }}
                  className="rounded border border-border bg-bg-elevated px-2.5 py-1 text-xs font-medium text-text-secondary hover:text-text-primary transition-colors"
                >
                  Copy Text
                </button>
                <button
                  type="button"
                  onClick={() => setReportModalContent(null)}
                  className="rounded border border-border bg-bg-elevated px-2.5 py-1 text-xs font-medium text-text-secondary hover:text-text-primary transition-colors"
                >
                  Close
                </button>
              </div>
            </div>
            <div className="scrollbar-thin flex-1 overflow-y-auto bg-bg-inset p-5 rounded-lg border border-border">
              <FormattedReport content={reportModalContent} />
            </div>
          </div>
        </div>
      )}
    </>
  );
};