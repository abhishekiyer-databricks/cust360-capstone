import {
  Anchor,
  Badge,
  Button,
  Card,
  Group,
  Loader,
  Stack,
  Text,
  Title,
} from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { DataTable } from "mantine-datatable";
import { useState } from "react";

import { JobRun, getJobRun, listJobRuns, runForwardEtl } from "../api/client";

// A run is done once it leaves the active lifecycle states.
const TERMINAL = new Set(["TERMINATED", "SKIPPED", "INTERNAL_ERROR"]);
const isTerminal = (r?: JobRun) => !!r && !!r.life_cycle_state && TERMINAL.has(r.life_cycle_state);

function StateBadge({ run }: { run: JobRun }) {
  // Before terminal, show the lifecycle state (RUNNING…); after, show the result (SUCCESS/FAILED).
  if (!isTerminal(run)) {
    return (
      <Group gap={6}>
        <Loader size="xs" color="lava" />
        <Badge color="blue" variant="light">
          {run.life_cycle_state ?? "PENDING"}
        </Badge>
      </Group>
    );
  }
  const result = run.result_state ?? run.life_cycle_state ?? "UNKNOWN";
  const color = result === "SUCCESS" ? "teal" : result === "FAILED" ? "red" : "gray";
  return (
    <Badge color={color} variant="light">
      {result}
    </Badge>
  );
}

const fmtTime = (ms: number | null) => (ms ? new Date(ms).toLocaleString() : "—");
const fmtDuration = (start: number | null, end: number | null) =>
  start && end ? `${Math.max(0, Math.round((end - start) / 1000))}s` : "—";

export default function Reports() {
  const qc = useQueryClient();
  const [activeRunId, setActiveRunId] = useState<number | null>(null);

  // Recent-runs history (also refreshed whenever a run finishes).
  const { data: runs, isLoading: runsLoading } = useQuery<JobRun[]>({
    queryKey: ["job-runs"],
    queryFn: listJobRuns,
    staleTime: 10 * 1000,
  });

  // Poll the active run until it reaches a terminal state, then stop.
  const { data: activeRun } = useQuery<JobRun>({
    queryKey: ["job-run", activeRunId],
    queryFn: () => getJobRun(activeRunId!),
    enabled: activeRunId != null,
    refetchInterval: (query) => (isTerminal(query.state.data) ? false : 3000),
  });

  // When the active run turns terminal, refresh the history table.
  if (activeRun && isTerminal(activeRun)) {
    qc.invalidateQueries({ queryKey: ["job-runs"] });
  }

  const trigger = useMutation({
    mutationFn: runForwardEtl,
    onSuccess: (res) => {
      setActiveRunId(res.run_id);
      qc.invalidateQueries({ queryKey: ["job-runs"] });
      notifications.show({ message: `Forward-ETL started (run ${res.run_id})`, color: "teal" });
    },
    onError: (e) => notifications.show({ message: (e as Error).message, color: "red" }),
  });

  return (
    <Stack>
      <Title order={2}>Reports</Title>

      <Card withBorder radius="md" padding="lg">
        <Group justify="space-between" align="center">
          <div>
            <Text fw={600}>Forward-ETL</Text>
            <Text size="sm" c="dimmed">
              Promote staged notes &amp; segment overrides from Lakebase into Delta gold.
            </Text>
          </div>
          <Button color="lava" onClick={() => trigger.mutate()} loading={trigger.isPending}>
            Run forward-ETL
          </Button>
        </Group>

        {activeRun && (
          <Group mt="md" gap="sm">
            <Text size="sm" c="dimmed">
              Run {activeRun.run_id}:
            </Text>
            <StateBadge run={activeRun} />
            {activeRun.run_page_url && (
              <Anchor href={activeRun.run_page_url} target="_blank" size="sm">
                Open in workspace ↗
              </Anchor>
            )}
          </Group>
        )}
      </Card>

      <Card withBorder radius="md" padding="lg">
        <Text fw={600} mb="sm">
          Recent runs
        </Text>
        {runsLoading ? (
          <Loader color="lava" />
        ) : (
          <DataTable<JobRun>
            withTableBorder
            borderRadius="md"
            striped
            minHeight={120}
            records={runs ?? []}
            idAccessor="run_id"
            noRecordsText="No runs yet"
            columns={[
              {
                accessor: "run_id",
                title: "Run",
                render: (r) =>
                  r.run_page_url ? (
                    <Anchor href={r.run_page_url} target="_blank">
                      {r.run_id}
                    </Anchor>
                  ) : (
                    r.run_id
                  ),
              },
              { accessor: "state", title: "Status", render: (r) => <StateBadge run={r} /> },
              { accessor: "start_time", title: "Started", render: (r) => fmtTime(r.start_time) },
              {
                accessor: "duration",
                title: "Duration",
                render: (r) => fmtDuration(r.start_time, r.end_time),
              },
            ]}
          />
        )}
      </Card>
    </Stack>
  );
}
