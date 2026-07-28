import { Alert, Box, Center, Loader } from "@mantine/core";
import { IconAlertTriangle } from "@tabler/icons-react";
import { useQuery } from "@tanstack/react-query";

import { getConfig } from "../api/client";

// T4 — Embed the provisioned AI/BI dashboard as an iframe.
//
// The dashboard authenticates the VIEWER itself (they're already logged into the workspace
// in the same browser), so there's no data/API plumbing here — just the supported embed URL
// `${host}/embed/dashboardsv3/${dashboard_id}`. host + id come from /api/config (never
// hardcoded). The workspace must allowlist the app domain under Settings → Security →
// External Access → Embed Dashboard, else the browser blocks the frame via X-Frame-Options.
export default function Dashboard() {
  // config is tiny + static → cache 5m (matches master_plan §7 config staleTime).
  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["config"],
    queryFn: getConfig,
    staleTime: 5 * 60 * 1000,
  });

  if (isLoading) {
    return (
      <Center h="70vh">
        <Loader color="lava" />
      </Center>
    );
  }

  if (isError || !data?.databricks_host || !data?.dashboard_id) {
    return (
      <Alert
        icon={<IconAlertTriangle size={18} />}
        color="red"
        variant="light"
        title="Dashboard unavailable"
      >
        {isError
          ? `Could not load app config: ${(error as Error).message}`
          : "Missing databricks_host or dashboard_id in /api/config."}
      </Alert>
    );
  }

  // Defense-in-depth: the backend already prepends https://, but guard here too — a
  // scheme-less host would make `src` a RELATIVE path that resolves to the app's own origin,
  // and the SPA catch-all would serve index.html → the iframe loads the app itself (infinite
  // nesting). Force an absolute origin so that can never happen.
  const host = /^https?:\/\//.test(data.databricks_host)
    ? data.databricks_host
    : `https://${data.databricks_host}`;
  const src = `${host}/embed/dashboardsv3/${data.dashboard_id}`;

  // Fill the AppShell.Main content area. AppShell padding is "md"; subtract header height so
  // the iframe fills the viewport without a scrollbar-in-scrollbar.
  return (
    <Box style={{ height: "calc(100vh - 56px - 2 * var(--mantine-spacing-md))" }}>
      <iframe
        title="Customer 360 AI/BI dashboard"
        src={src}
        style={{ width: "100%", height: "100%", border: 0, borderRadius: 8 }}
      />
    </Box>
  );
}
