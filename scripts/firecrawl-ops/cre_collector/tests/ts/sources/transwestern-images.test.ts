import test from "node:test";
import assert from "node:assert/strict";
import { filterTranswesternImages, isTranswesternTemplateImage } from "../../../sources/pure/transwestern-images.js";

const marker = "https://transwestern.com/assets/images/transwestern-mapmarker.png";
const mail = "https://images.transwestern.com/f00000000081388/transwestern.com/assets/images/mail-3.png";
const portrait = "https://images.transwestern.com/f00000000081388/s3.amazonaws.com/transwestern-userphoto/THAWP1.jpg";
const property = "https://images.transwestern.com/property/primary.jpg";

test("Transwestern template policy recognizes only owned observed asset paths", () => {
  for (const name of ["mail-3.png", "connect-image.png", "tw_gl.png", "transwestern-mapmarker.png"]) {
    assert.equal(isTranswesternTemplateImage(`https://transwestern.com/assets/images/${name}`), true);
    assert.equal(isTranswesternTemplateImage(`https://images.transwestern.com/f00000000081388/transwestern.com/assets/images/${name}`), true);
  }
  assert.equal(isTranswesternTemplateImage("/assets/images/tw_gl.png"), true);
  for (const url of [
    "https://transwestern.com.example.test/assets/images/tw_gl.png",
    "https://example.test/transwestern.com/assets/images/tw_gl.png",
    "https://images.transwestern.com.example.test/assets/images/tw_gl.png",
    "https://transwestern.com/property/images/tw_gl.png",
    "https://transwestern.com/assets/images/property.jpg",
    "https://transwestern.com/assets/images/mailbox-warehouse.jpg",
    "https://transwestern.com/assets/images/mail-3.png-extra",
    "https://transwestern.com/assets/images/comment.png",
    "https://transwestern.com/assets/images/tw-logo.png",
    "https://transwestern.com/assets/images/Transwestern_2023.png",
    "https://transwestern.com/other/assets/images/mail-3.png",
    "https://images.transwestern.com/property/transwestern-mapmarker.png",
    "https://images.transwestern.com/f00000000081388/example.test/assets/images/tw_gl.png",
    "ftp://transwestern.com/assets/images/tw_gl.png",
    "javascript:void(0)",
  ]) assert.equal(isTranswesternTemplateImage(url), false, url);
});

test("final harvest template ingress and exact parsed avatars are excluded with finite evidence", () => {
  const unknown = "https://images.transwestern.com/unknown/photo.jpg";
  const result = filterTranswesternImages([marker, property, mail, portrait, unknown], {
    nativePropertyUrls: [], brokerAvatarUrls: [portrait],
  });
  assert.deepEqual(result.images, [property, unknown]);
  assert.deepEqual(result.excluded, [
    { url: marker, role: "template_resource", basis: "existing_source_template_policy" },
    { url: mail, role: "template_resource", basis: "existing_source_template_policy" },
    { url: portrait, role: "broker_portrait", basis: "parsed_contact_avatar" },
  ]);
});

test("exact native property references outrank avatar and template roles without reintroducing absent URLs", () => {
  const result = filterTranswesternImages([portrait, property, marker], {
    nativePropertyUrls: [portrait, marker, "https://images.transwestern.com/property/absent.jpg"],
    brokerAvatarUrls: [portrait],
  });
  assert.deepEqual(result, { images: [portrait, property, marker], excluded: [] });
});

test("unknown URLs and order remain unchanged; filenames alone do not classify portraits", () => {
  const otherPortrait = "https://images.transwestern.com/f00000000081388/s3.amazonaws.com/transwestern-userphoto/OTHER.jpg";
  const unrelated = "https://example.test/assets/images/tw_gl.png";
  const images = [otherPortrait, unrelated, property, otherPortrait];
  assert.deepEqual(filterTranswesternImages(images, { nativePropertyUrls: [], brokerAvatarUrls: [portrait] }), { images, excluded: [] });
});

test("canonical exact matching supports relative owned references and deduplicates excluded URL entries", () => {
  assert.deepEqual(filterTranswesternImages([marker, marker, portrait, portrait], {
    nativePropertyUrls: [], brokerAvatarUrls: [portrait],
  }).excluded, [
    { url: marker, role: "template_resource", basis: "existing_source_template_policy" },
    { url: portrait, role: "broker_portrait", basis: "parsed_contact_avatar" },
  ]);
  assert.deepEqual(filterTranswesternImages([marker], {
    nativePropertyUrls: ["/assets/images/transwestern-mapmarker.png"], brokerAvatarUrls: [],
  }), { images: [marker], excluded: [] });
});

test("empty harvest stays empty even with native references", () => {
  assert.deepEqual(filterTranswesternImages([], { nativePropertyUrls: [property], brokerAvatarUrls: [portrait] }), { images: [], excluded: [] });
});

test("the same image has listing-local roles without cross-listing exclusions", () => {
  const listingA = filterTranswesternImages([portrait], {
    nativePropertyUrls: [], brokerAvatarUrls: [portrait],
  });
  assert.deepEqual(listingA, {
    images: [],
    excluded: [{ url: portrait, role: "broker_portrait", basis: "parsed_contact_avatar" }],
  });
  const listingB = filterTranswesternImages([portrait], {
    nativePropertyUrls: [portrait], brokerAvatarUrls: [portrait],
  });
  assert.deepEqual(listingB, { images: [portrait], excluded: [] });
});
