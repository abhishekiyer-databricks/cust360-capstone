import { Suspense, lazy } from "react";
import { Center, Loader } from "@mantine/core";
import { Navigate, Route, Routes } from "react-router-dom";

import AppLayout from "./components/AppLayout";

// Code-split routes so the initial bundle stays small (master_plan §7 React perf).
const Customers = lazy(() => import("./pages/Customers"));
const CustomerDetail = lazy(() => import("./pages/CustomerDetail"));
const Placeholder = lazy(() => import("./pages/Placeholder"));

export default function App() {
  return (
    <AppLayout>
      <Suspense
        fallback={
          <Center h="60vh">
            <Loader color="lava" />
          </Center>
        }
      >
        <Routes>
          <Route path="/" element={<Navigate to="/customers" replace />} />
          <Route path="/customers" element={<Customers />} />
          <Route path="/customers/:id" element={<CustomerDetail />} />
          <Route
            path="/dashboard"
            element={<Placeholder title="Dashboard" note="Embedded AI/BI dashboard — T4." />}
          />
          <Route
            path="/reports"
            element={<Placeholder title="Reports" note="Forward-ETL runs — T7." />}
          />
          <Route path="*" element={<Navigate to="/customers" replace />} />
        </Routes>
      </Suspense>
    </AppLayout>
  );
}
