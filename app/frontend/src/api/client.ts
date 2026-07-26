// Typed fetch wrapper + API functions. The Databricks Apps proxy handles auth (injects the
// user token / SP creds), so the browser just calls same-origin /api/* with no auth headers.

// ---- Types (mirror app/backend/models.py) --------------------------------
export interface Customer {
  customer_id: string;
  first_name: string | null;
  last_name: string | null;
  email: string | null;
  country: string | null;
  segment_id: string | null;
  lifetime_value: number | null;
  churn_score: number | null;
}

export interface Transaction {
  transaction_id: string;
  product_id: string | null;
  transaction_date: string | null;
  channel: string | null;
  status: string | null;
  amount: number | null;
}

export interface CustomerProfile {
  customer_id: string;
  first_name: string | null;
  last_name: string | null;
  email: string | null;
  phone: string | null;
  country: string | null;
  city: string | null;
  gender: string | null;
  age: number | null;
  signup_date: string | null;
  last_purchase_date: string | null;
  segment_id: string | null;
  lifetime_value: number | null;
  churn_score: number | null;
  updated_at: string | null;
}

export interface CustomerDetail {
  profile: CustomerProfile;
  recent_transactions: Transaction[];
}

export interface Page<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
}

export interface WhoAmI {
  identity: string;
  user_name: string | null;
  display_name: string | null;
  email_from_header: string | null;
}

// ---- Core fetch ----------------------------------------------------------
async function apiGet<T>(path: string): Promise<T> {
  const res = await fetch(`/api${path}`, {
    headers: { Accept: "application/json" },
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail ?? detail;
    } catch {
      /* non-JSON error body */
    }
    throw new Error(`${res.status}: ${detail}`);
  }
  return res.json() as Promise<T>;
}

// ---- API surface ---------------------------------------------------------
export interface CustomerFilters {
  segment?: string | null;
  min_ltv?: number | null;
  max_churn?: number | null;
  page: number;
  page_size: number;
}

export function listCustomers(f: CustomerFilters): Promise<Page<Customer>> {
  const q = new URLSearchParams();
  if (f.segment) q.set("segment", f.segment);
  if (f.min_ltv != null) q.set("min_ltv", String(f.min_ltv));
  if (f.max_churn != null) q.set("max_churn", String(f.max_churn));
  q.set("page", String(f.page));
  q.set("page_size", String(f.page_size));
  return apiGet<Page<Customer>>(`/customers?${q.toString()}`);
}

export function getCustomer(id: string): Promise<CustomerDetail> {
  return apiGet<CustomerDetail>(`/customers/${encodeURIComponent(id)}`);
}

export function whoami(): Promise<WhoAmI> {
  return apiGet<WhoAmI>("/whoami");
}
