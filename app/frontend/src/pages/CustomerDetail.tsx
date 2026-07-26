import {
  Anchor,
  Badge,
  Card,
  Group,
  Loader,
  Progress,
  SimpleGrid,
  Stack,
  Tabs,
  Text,
  Title,
} from "@mantine/core";
import { useQuery } from "@tanstack/react-query";
import { DataTable } from "mantine-datatable";
import { Link, useParams } from "react-router-dom";

import {
  CustomerMetrics,
  CustomerProfile,
  Transaction,
  getCustomer,
  getCustomerMetrics,
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
            <Tabs.Tab value="notes" disabled>
              Notes (T3C)
            </Tabs.Tab>
            <Tabs.Tab value="segment" disabled>
              Segment (T3C)
            </Tabs.Tab>
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
        </Tabs>
      )}
    </Stack>
  );
}
