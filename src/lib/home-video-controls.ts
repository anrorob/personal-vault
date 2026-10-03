export type HomeVideoQuery = {
  include_hidden: boolean;
  include_shared: boolean;
  sort: "newest" | "oldest" | "name_asc" | "name_desc";
  person: string;
  private_tag: string;
  content_tag: string;
  date_from: string;
  date_to: string;
  location: string;
};
export const initialHomeVideoQuery: HomeVideoQuery = {
  include_hidden: false,
  include_shared: true,
  sort: "newest",
  person: "",
  private_tag: "",
  content_tag: "",
  date_from: "",
  date_to: "",
  location: "",
};
export function homeVideoSearch(query: HomeVideoQuery) {
  return new URLSearchParams(
    Object.entries(query)
      .filter(([, value]) => value !== "")
      .map(([key, value]) => [key, String(value)]),
  ).toString();
}
