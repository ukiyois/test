import { createRoot } from "react-dom/client";
import { App } from "./App";

import "./styles/tokens.css";
import "./styles/base.css";
import "./styles/components.css";
import "./styles/shell.css";
import "./styles/chat.css";
import "./styles/workflow.css";

createRoot(document.getElementById("root")!).render(<App />);
