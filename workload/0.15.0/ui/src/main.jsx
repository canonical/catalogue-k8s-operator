import "./styles/vanilla.scss";
import "./styles/ui.css";
import { render } from "preact";
import { useState, useEffect } from "preact/hooks";
import { App } from "./components/App";

function Root() {
  const [config, setConfig] = useState(null);

  useEffect(() => {
    fetch("config.json")
      .then((res) => res.json())
      .then(setConfig);
  }, []);

  if (!config) return null;

  return <App config={config} />;
}

render(<Root />, document.getElementById("root"));
