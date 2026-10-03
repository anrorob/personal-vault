import { afterEach, expect, test } from "bun:test";
import {
  clearGalleryPosition,
  galleryReturnPosition,
  rememberGalleryPosition,
  visitGalleryPath,
} from "../src/lib/gallery";

afterEach(clearGalleryPosition);
const position = {
  assetId: "10000000-0000-0000-0000-000000000001",
  photoId: "synthetic-deep",
  query: "sort=oldest&photo_type=camera",
  offset: -37,
  hidden: true,
};

test("logical position is used only on a detail return and is cleared on leaving Gallery", () => {
  visitGalleryPath("/app/gallery");
  rememberGalleryPosition(position);
  expect(galleryReturnPosition()).toBeNull();
  visitGalleryPath("/app/gallery/synthetic-deep");
  expect(galleryReturnPosition()).toBeNull();
  visitGalleryPath("/app/gallery/");
  expect(galleryReturnPosition()).toEqual(position);
  // Repeated layout renders retain the return decision.
  visitGalleryPath("/app/gallery/");
  expect(galleryReturnPosition()).toEqual(position);
  clearGalleryPosition();
  visitGalleryPath("/app/gallery/");
  expect(galleryReturnPosition()).toBeNull();
});

test("detail sibling navigation retains the originally opened Gallery anchor", () => {
  visitGalleryPath("/app/gallery/");
  rememberGalleryPosition(position);
  visitGalleryPath("/app/gallery/synthetic-deep");
  visitGalleryPath("/app/gallery/synthetic-next");
  visitGalleryPath("/app/gallery");
  expect(galleryReturnPosition()).toEqual(position);
});
