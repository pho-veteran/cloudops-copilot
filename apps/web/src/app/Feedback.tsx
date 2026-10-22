import { Button } from "@/components/ui/button";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { message } from "@/lib/api/client";

export function Failure({
  error,
  retry,
}: {
  error: unknown;
  retry?: () => void;
}) {
  return (
    <Alert variant="destructive" role="alert">
      <AlertTitle>Unable to load or save</AlertTitle>
      <AlertDescription>
        {message(error)}{" "}
        {retry && (
          <Button variant="outline" onClick={retry}>
            Retry
          </Button>
        )}
      </AlertDescription>
    </Alert>
  );
}
export function Loading() {
  return <p role="status">Loading saved application data…</p>;
}
