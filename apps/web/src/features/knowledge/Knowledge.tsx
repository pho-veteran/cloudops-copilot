import { useEffect, useRef, useState } from "react";
import {
  buildIncidentHash,
  buildKnowledgeHash,
  type KnowledgeRoute,
} from "@/app/routes";
import { Failure, Loading } from "@/app/Feedback";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { download } from "@/lib/download";
import { KnowledgeStatus } from "./KnowledgeStatus";
import { useExactRunbook, useKnowledgeCatalog } from "./queries";

function Filters({ route }: { route: KnowledgeRoute }) {
  const [search, setSearch] = useState(route.search ?? "");
  const [tags, setTags] = useState(route.tags?.join(", ") ?? "");
  const [problem, setProblem] = useState("");
  return (
    <form
      className="knowledge-filters foundation-panel"
      onSubmit={(event) => {
        event.preventDefault();
        const values = [
          ...new Set(
            tags
              .split(",")
              .map((tag) => tag.trim())
              .filter(Boolean),
          ),
        ];
        if (values.length > 16 || values.some((tag) => tag.length > 64)) {
          setProblem("Use at most 16 tags, each at most 64 characters.");
          return;
        }
        setProblem("");
        window.location.hash = buildKnowledgeHash({
          search: search.trim(),
          tags: values,
          returnTo: route.returnTo,
        });
      }}
    >
      <div>
        <Label htmlFor="knowledge-search">Search guidance</Label>
        <Input
          id="knowledge-search"
          value={search}
          maxLength={2000}
          onChange={(e) => setSearch(e.target.value)}
        />
      </div>
      <div>
        <Label htmlFor="knowledge-tags">Tags (comma-separated)</Label>
        <Input
          id="knowledge-tags"
          value={tags}
          maxLength={2048}
          onChange={(e) => setTags(e.target.value)}
        />
      </div>
      <Button type="submit">Search</Button>
      {problem && <p role="alert">{problem}</p>}
    </form>
  );
}

function Catalog({ route }: { route: KnowledgeRoute }) {
  const [cursors, setCursors] = useState<(string | undefined)[]>([undefined]);
  const query = useKnowledgeCatalog(route.search, route.tags, cursors.at(-1));
  return (
    <aside className="foundation-panel">
      <h2>Current catalog</h2>
      {query.isPending ? (
        <Loading />
      ) : query.isError ? (
        <>
          <Failure error={query.error} retry={() => void query.refetch()} />
          {cursors.length > 1 && (
            <Button variant="outline" onClick={() => setCursors([undefined])}>
              Start from first page
            </Button>
          )}
        </>
      ) : (
        <>
          {!query.data.items.length && (
            <p role="status">
              {query.data.catalog_empty_reason === "not_indexed"
                ? "No catalog has been indexed yet."
                : query.data.catalog_empty_reason === "no_matches"
                  ? "No runbooks match these filters."
                  : "The current indexed catalog is empty."}
            </p>
          )}
          <ol className="foundation-stack knowledge-catalog">
            {query.data.items.map((item) => (
              <li key={item.id}>
                <a
                  aria-current={route.document === item.id ? "page" : undefined}
                  href={buildKnowledgeHash({
                    search: route.search,
                    tags: route.tags,
                    document: item.id,
                    returnTo: route.returnTo,
                  })}
                >
                  {item.title}
                </a>
                <p className="muted">
                  Version {item.version} ·{" "}
                  {item.guide_kind.replaceAll("_", " ")}
                </p>
                <p className="muted">{item.tags.join(" · ")}</p>
              </li>
            ))}
          </ol>
          <div className="foundation-actions">
            <Button
              size="sm"
              variant="outline"
              disabled={cursors.length === 1}
              onClick={() => setCursors((values) => values.slice(0, -1))}
            >
              Previous page
            </Button>
            <Button
              size="sm"
              variant="outline"
              disabled={!query.data.next_cursor}
              onClick={() =>
                setCursors((values) => [...values, query.data!.next_cursor!])
              }
            >
              Next page
            </Button>
          </div>
        </>
      )}
    </aside>
  );
}

function Reader({ route }: { route: KnowledgeRoute }) {
  const exact = useExactRunbook(route);
  const current = useKnowledgeCatalog("", [], undefined, 100);
  const focus = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    if (exact.data?.chunk) focus.current?.focus();
  }, [exact.data?.chunk?.id]);
  if (exact.isPending)
    return (
      <article className="foundation-panel">
        <Loading />
      </article>
    );
  if (exact.isError)
    return (
      <article className="foundation-panel">
        <Failure error={exact.error} retry={() => void exact.refetch()} />
        <p>
          The exact requested version remains selected. No current version is
          substituted.
        </p>
      </article>
    );
  const { document, chunk } = exact.data;
  const { content, ...provenance } = document;
  const name = `${document.document_key}-v${document.version.replace(/[^a-z0-9._-]/gi, "_")}`;
  return (
    <article className="foundation-panel knowledge-reader">
      <div className="foundation-actions">
        <a
          href={buildKnowledgeHash({
            search: route.search,
            tags: route.tags,
            returnTo: route.returnTo,
          })}
        >
          ← Catalog with these filters
        </a>
        {(route.returnTo || (route.incident && route.run)) && (
          <a
            href={
              route.returnTo ?? buildIncidentHash(route.incident!, route.run!)
            }
          >
            ← Back to selected Run
          </a>
        )}
      </div>
      <h2>{document.title}</h2>
      <div className="foundation-actions">
        <Badge variant="outline">Version {document.version}</Badge>
        <Badge variant="outline">
          {route.selection ? "Saved Run version" : "Exact stored version"}
        </Badge>
      </div>
      {current.isPending ? (
        <p role="status">Checking current catalog membership…</p>
      ) : current.isError ? (
        <p>
          Current catalog membership is unavailable; this exact stored version
          remains readable.{" "}
          <Button
            variant="outline"
            size="sm"
            onClick={() => void current.refetch()}
          >
            Retry current membership
          </Button>
        </p>
      ) : (
        <p>
          {current.data.items.some((item) => item.id === document.id)
            ? "Current indexed version"
            : "Historical version · retired from the current catalog"}
        </p>
      )}
      <dl className="foundation-metadata">
        <dt>Source</dt>
        <dd className="mono">{document.source_path}</dd>
        <dt>Document version ID</dt>
        <dd className="mono">{document.id}</dd>
        <dt>Applicability</dt>
        <dd>
          {document.applicability.kind} ·{" "}
          {document.applicability.symptom_families.join(", ") ||
            "Generally applicable"}
        </dd>
        <dt>Recovery templates</dt>
        <dd>
          {document.applicability.recovery_templates.join(", ") ||
            "Diagnosis / manual guidance only"}
        </dd>
        <dt>Reviewed source</dt>
        <dd className="mono">{document.provenance.source_revision}</dd>
        <dt>Review date</dt>
        <dd>{document.provenance.reviewed_at}</dd>
        <dt>Content SHA-256</dt>
        <dd className="mono">{document.content_sha256}</dd>
        <dt>Metadata SHA-256</dt>
        <dd className="mono">{document.metadata_sha256}</dd>
      </dl>
      <div className="foundation-actions">
        <Button
          variant="outline"
          onClick={() =>
            download(content, `${name}.md`, "text/markdown;charset=utf-8")
          }
        >
          Download exact document
        </Button>
        <Button
          variant="outline"
          onClick={() =>
            download(
              JSON.stringify(provenance, null, 2),
              `${name}.provenance.json`,
              "application/json",
            )
          }
        >
          Download provenance
        </Button>
      </div>
      <p className="muted">
        Methodology only. Content is displayed as inert text; links, scripts and
        images inside it are not activated.
      </p>
      {chunk ? (
        <>
          <h2 ref={focus} tabIndex={-1}>
            Exact stored chunk
          </h2>
          <p>
            {chunk.heading_path.join(" / ") || "Document text"} · chunk{" "}
            {chunk.ordinal + 1} · bytes {chunk.start_byte}–{chunk.end_byte}
          </p>
          <p className="mono">
            {chunk.id} · {chunk.content_sha256}
          </p>
          <pre className="knowledge-text">{chunk.content}</pre>
          <details>
            <summary>Full stored document</summary>
            <pre className="knowledge-text">{content}</pre>
          </details>
        </>
      ) : (
        <pre className="knowledge-text">{content}</pre>
      )}
    </article>
  );
}

export function Knowledge({ route }: { route: KnowledgeRoute }) {
  const filterKey = JSON.stringify([route.search, route.tags]);
  return (
    <section className="foundation-stack">
      <div>
        <div className="eyebrow">PERSISTED GUIDANCE</div>
        <h1>Knowledge</h1>
        <p className="muted">
          Browse current runbooks or read an exact saved version. Guidance is
          methodology, not incident telemetry, a diagnosis or execution
          approval.
        </p>
      </div>
      <KnowledgeStatus />
      <Filters key={filterKey} route={route} />
      <div className="foundation-workspace">
        <Catalog key={filterKey} route={route} />
        {route.document ? (
          <Reader
            key={JSON.stringify([route.document, route.chunk, route.selection])}
            route={route}
          />
        ) : (
          <article className="foundation-panel">
            <h2>Select a runbook</h2>
            <p>
              Choose a current document, or open an exact historical reference
              from a saved Run.
            </p>
            {route.returnTo && (
              <a href={route.returnTo}>← Back to selected Run</a>
            )}
          </article>
        )}
      </div>
    </section>
  );
}
