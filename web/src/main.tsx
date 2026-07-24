import "@mantine/core/styles.css";
import "@mantine/charts/styles.css";

import { mountCanvas } from "@thisismydesign/cursor-canvas-web/runtime";
import MemebotCandidates from "../../canvases/memebot-candidates.canvas";

mountCanvas("root", <MemebotCandidates />);
