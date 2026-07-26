import {
  Anchor,
  Badge,
  Card,
  Group,
  Loader,
  SimpleGrid,
  Stack,
  Tabs,
  Text,
  Title,
} from "@mantine/core";
import { useQuery } from "@tanstack/react-query";
import { DataTable } from "mantine-datatable";
import { Link, useParams } from "react-router-dom";

import { CustomerProfile, Transaction, getCustomer } from "../api/client";

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
            <Tabs.Tab value="metrics" disabled>
              Metrics (T3B)
            </Tabs.Tab>
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
        </Tabs>
      )}
    </Stack>
  );
}
