import { useState } from "react";
import { Badge, Group, NumberInput, Paper, Stack, TextInput, Title } from "@mantine/core";
import { useDebouncedValue } from "@mantine/hooks";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { DataTable } from "mantine-datatable";
import { useNavigate } from "react-router-dom";

import { Customer, listCustomers } from "../api/client";

const PAGE_SIZE = 25;

function money(v: number | null): string {
  return v == null ? "—" : `$${v.toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
}

function churnBadge(v: number | null) {
  if (v == null) return <Badge variant="light">—</Badge>;
  const color = v >= 0.7 ? "red" : v >= 0.4 ? "yellow" : "teal";
  return <Badge color={color} variant="light">{v.toFixed(2)}</Badge>;
}

export default function Customers() {
  const navigate = useNavigate();
  const [page, setPage] = useState(1);

  // Filter inputs. Debounce ~250ms so typing doesn't fire a request per keystroke
  // (master_plan §7 React perf).
  const [segment, setSegment] = useState("");
  const [minLtv, setMinLtv] = useState<number | "">("");
  const [maxChurn, setMaxChurn] = useState<number | "">("");
  const [debounced] = useDebouncedValue({ segment, minLtv, maxChurn }, 250);

  const filters = {
    segment: debounced.segment || null,
    min_ltv: debounced.minLtv === "" ? null : Number(debounced.minLtv),
    max_churn: debounced.maxChurn === "" ? null : Number(debounced.maxChurn),
    page,
    page_size: PAGE_SIZE,
  };

  const { data, isFetching } = useQuery({
    queryKey: ["customers", filters],
    queryFn: () => listCustomers(filters),
    staleTime: 10 * 1000, // list: 10s (master_plan §7)
    placeholderData: keepPreviousData, // keep last page visible while the next loads
  });

  return (
    <Stack>
      <Title order={2}>Customers</Title>

      <Paper withBorder radius="md" p="md">
        <Group align="end">
          <TextInput
            label="Segment"
            placeholder="e.g. S or S1"
            value={segment}
            onChange={(e) => {
              setSegment(e.currentTarget.value);
              setPage(1);
            }}
            w={140}
          />
          <NumberInput
            label="Min lifetime value"
            placeholder="0"
            value={minLtv}
            onChange={(v) => {
              setMinLtv(v === "" ? "" : Number(v));
              setPage(1);
            }}
            min={0}
            w={180}
          />
          <NumberInput
            label="Max churn score"
            placeholder="1.0"
            value={maxChurn}
            onChange={(v) => {
              setMaxChurn(v === "" ? "" : Number(v));
              setPage(1);
            }}
            min={0}
            max={1}
            step={0.1}
            decimalScale={2}
            w={180}
          />
        </Group>
      </Paper>

      <DataTable<Customer>
        withTableBorder
        borderRadius="md"
        striped
        highlightOnHover
        minHeight={200}
        fetching={isFetching}
        records={data?.items ?? []}
        idAccessor="customer_id"
        onRowClick={({ record }) => navigate(`/customers/${record.customer_id}`)}
        columns={[
          { accessor: "customer_id", title: "ID", width: 110 },
          {
            accessor: "name",
            title: "Name",
            render: (c) => `${c.first_name ?? ""} ${c.last_name ?? ""}`.trim() || "—",
          },
          { accessor: "email", title: "Email" },
          { accessor: "country", title: "Country", width: 110 },
          { accessor: "segment_id", title: "Segment", width: 100 },
          {
            accessor: "lifetime_value",
            title: "LTV",
            width: 120,
            textAlign: "right",
            render: (c) => money(c.lifetime_value),
          },
          {
            accessor: "churn_score",
            title: "Churn",
            width: 100,
            render: (c) => churnBadge(c.churn_score),
          },
        ]}
        totalRecords={data?.total ?? 0}
        recordsPerPage={PAGE_SIZE}
        page={page}
        onPageChange={setPage}
      />
    </Stack>
  );
}
