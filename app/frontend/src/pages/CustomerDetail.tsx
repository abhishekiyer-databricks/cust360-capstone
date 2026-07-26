import { useState } from "react";
import {
  Anchor,
  Badge,
  Button,
  Card,
  Group,
  Loader,
  Progress,
  Select,
  SimpleGrid,
  Stack,
  Text,
  Textarea,
  Tabs,
  Title,
} from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { DataTable } from "mantine-datatable";
import { Link, useParams } from "react-router-dom";

import {
  CustomerMetrics,
  CustomerProfile,
  Note,
  Transaction,
  addNote,
  getCustomer,
  getCustomerMetrics,
  listNotes,
  listSegments,
  overrideSegment,
} from "../api/client";

const fmtMoney = (v: number) => `$${v.toLocaleString(undefined, { maximumFractionDigits: 2 })}`;

function Field({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div>
      <Text size="xs" c="dimmed" tt="uppercase" fw={600}>
        {label}
      </Text>
      <Text>{value ?? "—"}</Text>
    </div>
  );
}

function ProfileTab({ p }: { p: CustomerProfile }) {
  return (
    <Card withBorder radius="md" p="lg">
      <SimpleGrid cols={{ base: 1, sm: 2, md: 3 }} spacing="lg">
        <Field label="Name" value={`${p.first_name ?? ""} ${p.last_name ?? ""}`.trim()} />
        <Field label="Email" value={p.email} />
        <Field label="Phone" value={p.phone} />
        <Field label="Location" value={[p.city, p.country].filter(Boolean).join(", ")} />
        <Field label="Age / Gender" value={[p.age, p.gender].filter(Boolean).join(" / ")} />
        <Field label="Segment" value={p.segment_id} />
        <Field
          label="Lifetime value"
          value={p.lifetime_value == null ? "—" : `$${p.lifetime_value.toLocaleString()}`}
        />
        <Field
          label="Churn score"
          value={
            p.churn_score == null ? (
              "—"
            ) : (
              <Badge color={p.churn_score >= 0.7 ? "red" : p.churn_score >= 0.4 ? "yellow" : "teal"}>
                {p.churn_score.toFixed(2)}
              </Badge>
            )
          }
        />
        <Field label="Signup date" value={p.signup_date} />
        <Field label="Last purchase" value={p.last_purchase_date} />
      </SimpleGrid>
    </Card>
  );
}

function Stat({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <Card withBorder radius="md" padding="md">
      <Text size="xs" c="dimmed" tt="uppercase" fw={600}>
        {label}
      </Text>
      <Text size="xl" fw={700}>
        {value}
      </Text>
    </Card>
  );
}

function MetricsTab({ id }: { id: string }) {
  // Independent query → this tab loads in parallel with the detail fetch (master_plan §7).
  // Metrics are the expensive warehouse+OBO aggregate, so cache longer (60s).
  const { data, isLoading, error } = useQuery<CustomerMetrics>({
    queryKey: ["customer", id, "metrics"],
    queryFn: () => getCustomerMetrics(id),
    staleTime: 60 * 1000,
  });

  if (isLoading) return <Loader color="lava" />;
  if (error) return <Text c="red">Failed to load metrics: {(error as Error).message}</Text>;
  if (!data) return null;

  const maxCat = Math.max(...data.top_categories.map((c) => c.amount), 1);

  return (
    <Stack>
      <SimpleGrid cols={{ base: 2, sm: 3, md: 5 }}>
        <Stat label="Lifetime spend" value={fmtMoney(data.lifetime_spend)} />
        <Stat label="Last 30 days" value={fmtMoney(data.spend_30d)} />
        <Stat label="Last 90 days" value={fmtMoney(data.spend_90d)} />
        <Stat label="Open tickets" value={data.open_tickets} />
        <Stat label="Avg CSAT" value={data.avg_csat == null ? "—" : `${data.avg_csat} / 4`} />
      </SimpleGrid>

      <Card withBorder radius="md" padding="lg">
        <Text fw={600} mb="sm">
          Top categories by spend
        </Text>
        {data.top_categories.length === 0 && <Text c="dimmed">No completed purchases.</Text>}
        <Stack gap="xs">
          {data.top_categories.map((c) => (
            <div key={c.category ?? "unknown"}>
              <Group justify="space-between" mb={2}>
                <Text size="sm">{c.category ?? "Uncategorized"}</Text>
                <Text size="sm" c="dimmed">
                  {fmtMoney(c.amount)}
                </Text>
              </Group>
              <Progress value={(c.amount / maxCat) * 100} color="lava" size="sm" />
            </div>
          ))}
        </Stack>
      </Card>
    </Stack>
  );
}

function ActivityTab({ txns }: { txns: Transaction[] }) {
  return (
    <DataTable<Transaction>
      withTableBorder
      borderRadius="md"
      striped
      minHeight={150}
      records={txns}
      idAccessor="transaction_id"
      noRecordsText="No recent transactions"
      columns={[
        { accessor: "transaction_date", title: "Date", width: 130 },
        { accessor: "transaction_id", title: "Transaction" },
        { accessor: "product_id", title: "Product", width: 120 },
        { accessor: "channel", title: "Channel", width: 110 },
        {
          accessor: "status",
          title: "Status",
          width: 120,
          render: (t) => (
            <Badge
              variant="light"
              color={t.status === "completed" ? "teal" : t.status === "cancelled" ? "red" : "gray"}
            >
              {t.status ?? "—"}
            </Badge>
          ),
        },
        {
          accessor: "amount",
          title: "Amount",
          width: 110,
          textAlign: "right",
          render: (t) => (t.amount == null ? "—" : `$${t.amount.toFixed(2)}`),
        },
      ]}
    />
  );
}

function NotesTab({ id }: { id: string }) {
  const qc = useQueryClient();
  const [text, setText] = useState("");

  const { data: notes, isLoading } = useQuery<Note[]>({
    queryKey: ["customer", id, "notes"],
    queryFn: () => listNotes(id),
    staleTime: 10 * 1000,
  });

  const mutation = useMutation({
    mutationFn: () => addNote(id, text.trim()),
    onSuccess: () => {
      setText("");
      // Refetch the notes list so the new note shows immediately (master_plan §7).
      qc.invalidateQueries({ queryKey: ["customer", id, "notes"] });
      notifications.show({ message: "Note added", color: "teal" });
    },
    onError: (e) => notifications.show({ message: (e as Error).message, color: "red" }),
  });

  return (
    <Stack>
      <Card withBorder radius="md" padding="md">
        <Textarea
          label="Add a note"
          placeholder="Write a note about this customer…"
          value={text}
          onChange={(e) => setText(e.currentTarget.value)}
          autosize
          minRows={2}
        />
        <Group justify="flex-end" mt="sm">
          <Button
            color="lava"
            onClick={() => mutation.mutate()}
            loading={mutation.isPending}
            disabled={!text.trim()}
          >
            Add note
          </Button>
        </Group>
      </Card>

      {isLoading ? (
        <Loader color="lava" />
      ) : (notes ?? []).length === 0 ? (
        <Text c="dimmed">No notes yet.</Text>
      ) : (
        <Stack gap="sm">
          {notes!.map((n) => (
            <Card key={n.note_id} withBorder radius="md" padding="md">
              <Group justify="space-between" mb={4}>
                <Text size="sm" fw={600}>
                  {n.author_email}
                </Text>
                <Text size="xs" c="dimmed">
                  {new Date(n.created_at).toLocaleString()}
                </Text>
              </Group>
              <Text style={{ whiteSpace: "pre-wrap" }}>{n.note_text}</Text>
            </Card>
          ))}
        </Stack>
      )}
    </Stack>
  );
}

function SegmentTab({ id, currentSegment }: { id: string; currentSegment: string | null }) {
  const qc = useQueryClient();
  const [seg, setSeg] = useState<string | null>(null);
  const [reason, setReason] = useState("");

  const { data: segments } = useQuery({
    queryKey: ["segments"],
    queryFn: listSegments,
    staleTime: 5 * 60 * 1000,
  });

  const mutation = useMutation({
    mutationFn: () => overrideSegment(id, seg!, reason.trim() || undefined),
    onSuccess: (res) => {
      // Re-fetch the profile so the displayed current segment updates if it changed.
      qc.invalidateQueries({ queryKey: ["customer", id] });
      notifications.show({
        message: res.changed
          ? `Segment override saved (${res.override_segment})`
          : "No change — that override was already set",
        color: res.changed ? "teal" : "gray",
      });
    },
    onError: (e) => notifications.show({ message: (e as Error).message, color: "red" }),
  });

  const options = (segments ?? []).map((s) => ({
    value: s.segment_id,
    label: s.segment_name ? `${s.segment_name} (${s.segment_id})` : s.segment_id,
  }));

  return (
    <Card withBorder radius="md" padding="lg" maw={480}>
      <Text size="sm" c="dimmed" tt="uppercase" fw={600}>
        Current segment
      </Text>
      <Text size="lg" fw={700} mb="md">
        {currentSegment ?? "—"}
      </Text>

      <Select
        label="Override segment"
        placeholder="Choose a segment"
        data={options}
        value={seg}
        onChange={setSeg}
        mb="sm"
      />
      <Textarea
        label="Reason (optional)"
        placeholder="Why override?"
        value={reason}
        onChange={(e) => setReason(e.currentTarget.value)}
        autosize
        minRows={2}
        mb="sm"
      />
      <Group justify="flex-end">
        <Button
          color="lava"
          onClick={() => mutation.mutate()}
          loading={mutation.isPending}
          disabled={!seg}
        >
          Save override
        </Button>
      </Group>
    </Card>
  );
}

export default function CustomerDetail() {
  const { id = "" } = useParams();

  // Detail read. In 3B/3C this page fans out additional parallel useQuery calls (metrics,
  // notes) — each independent so the tabs load concurrently (master_plan §7).
  const { data, isLoading, error } = useQuery({
    queryKey: ["customer", id],
    queryFn: () => getCustomer(id),
    staleTime: 30 * 1000, // detail: 30s
  });

  return (
    <Stack>
      <Group justify="space-between">
        <Title order={2}>
          {data ? `${data.profile.first_name ?? ""} ${data.profile.last_name ?? ""}`.trim() : id}
        </Title>
        <Anchor component={Link} to="/customers" size="sm">
          ← Back to customers
        </Anchor>
      </Group>

      {isLoading && <Loader color="lava" />}
      {error && <Text c="red">Failed to load customer: {(error as Error).message}</Text>}

      {data && (
        <Tabs defaultValue="profile" color="lava">
          <Tabs.List>
            <Tabs.Tab value="profile">Profile</Tabs.Tab>
            <Tabs.Tab value="activity">Activity</Tabs.Tab>
            <Tabs.Tab value="metrics">Metrics</Tabs.Tab>
            <Tabs.Tab value="notes">Notes</Tabs.Tab>
            <Tabs.Tab value="segment">Segment</Tabs.Tab>
          </Tabs.List>

          <Tabs.Panel value="profile" pt="md">
            <ProfileTab p={data.profile} />
          </Tabs.Panel>
          <Tabs.Panel value="activity" pt="md">
            <ActivityTab txns={data.recent_transactions} />
          </Tabs.Panel>
          <Tabs.Panel value="metrics" pt="md">
            <MetricsTab id={id} />
          </Tabs.Panel>
          <Tabs.Panel value="notes" pt="md">
            <NotesTab id={id} />
          </Tabs.Panel>
          <Tabs.Panel value="segment" pt="md">
            <SegmentTab id={id} currentSegment={data.profile.segment_id} />
          </Tabs.Panel>
        </Tabs>
      )}
    </Stack>
  );
}
