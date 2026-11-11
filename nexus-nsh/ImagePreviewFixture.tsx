import { mount, View } from "nsh";
import { imagePreview } from "./src/store";
import { ImagePreview } from "./src/ui/image-preview";

imagePreview.set({ name: "a-very-long-image-name-that-should-not-push-the-close-button-off-screen.png", src: "data:image/png;base64,iVBORw0KGgo=" });

function Fixture() {
  return <View class="flex-1 bg-background"><ImagePreview /></View>;
}
mount(Fixture);
