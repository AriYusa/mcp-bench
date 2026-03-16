\section{Related Work}
\label{sec:related_work}

\subsection{Single-Agent Systems}

A single agent can be formalised as a tuple $\mathcal{A} = (M, E, S, A, \pi)$, where $M$ is the underlying language model, $E$ is the environment, $S$ is the state space, $A$ is the action space, and $\pi: S \rightarrow A$ is the policy function~\cite{lei2025}. The agent operates through an iterative perception--reasoning--action loop: at each timestep $t$, the agent receives observation $o_t$, combines it with memory $m_t$ and goal $g$ to form state $s_t = (o_t, m_t, g)$, and the LLM selects action $a_t = \pi(s_t)$~\cite{lei2025}.

The concept of an autonomous agent traces back to distributed artificial intelligence research of the 1980s. \cite{wooldridge1995} provided the classical definition of intelligent agents as systems that perceive their surroundings, make decisions, and take actions in response---a formulation that remains the baseline against which LLM-based agents are evaluated~\cite{guo2024}.

The emergence of large language models transformed this landscape by replacing narrow, task-specific interfaces with natural language. \cite{park2023} observe that generative agents offer natural language interfaces that make human interaction more flexible and easier to explain~\cite{park2023}---a sharp contrast with classical multi-agent or reinforcement learning systems. Internally, an LLM-based agent is commonly described as comprising three components: a \emph{brain} (the LLM with short- and long-term memory), a \emph{perception} module (converting multimodal inputs to prompts), and an \emph{action} module (text output plus external tool or API calls)~\cite{xi2023}. A complementary framing~\cite{weng2023} decomposes the supporting infrastructure into planning (subgoal decomposition and reflection), memory (in-context and external stores), and tool use (calling APIs for information absent from model weights).

\paragraph{Memory.}
\cite{han2024} define single-agent memory as a combination of short-term memory (ephemeral, scoped to the current interaction), long-term memory (chat histories and extracted facts stored in vector databases), and external retrieval via RAG~\cite{lewis2020,han2024}. \cite{shinn2023} add a fourth mechanism, Reflexion, in which self-reflective summaries of past attempts are prepended as episodic memory to subsequent turns, enabling verbal reinforcement without weight updates~\cite{shinn2023}.

\paragraph{Context window constraints.}
A fundamental constraint on all LLM-based agents is the context window. Because LLMs are stateless, the agent must assemble all relevant information---observations, memory, and goal---into a single prompt at each step; $E$ is therefore constrained by the number of tokens~\cite{tran2025}. Context window sizes have grown from 512 tokens in early transformers to 100--200K tokens in most current models, with some extending to 10M tokens~\cite{lei2025}. Despite this growth, several problems persist: quadratic attention complexity makes very long contexts computationally expensive; empirical studies show degraded performance on information located in the middle of long contexts (the ``lost-in-the-middle'' effect); and large contexts cost significantly more to process, which may be economically unreasonable for many applications~\cite{lei2025}.

These constraints define a scaling limit for single-agent systems. Long-horizon tasks that require retaining and updating state across many subtasks stress both context length and attention mechanisms, and orchestrating many heterogeneous capabilities within one prompt increases reasoning overhead. This motivates the use of multi-agent systems, among other approaches.

% NOTE: The draft mentions ``several ways to tackle the problem of limited context'' and gestures at a general description of methods before introducing multi-agent systems. Consider adding 1--2 sentences here listing other approaches (e.g., retrieval augmentation, hierarchical summarisation, memory compression) so the transition to multi-agent is well-motivated rather than abrupt.

\subsection{Multi-Agent Systems}

A multi-agent system (MAS) is characterised by distributed decision-making, where each agent independently perceives its environment and makes decisions, and agents interact through cooperation, competition, or hierarchical organisation~\cite{springer2024survey}. The definition used across the literature is broadly consistent: a collection of generative agents capable of interacting and collaborating within a shared environment~\cite{wang2024}. \cite{tran2025} extend this formally: $\mathcal{A} = \{a_i\}_{i=1}^n$ is a set of $n$ agents, $O_{\text{collab}}$ is a collective goal partitioned into per-agent objectives, $E$ is a shared environment (e.g., vector databases, messaging interfaces), and $C = \{c_j\}$ is a set of collaboration channels that distinguish mechanism, type, structure, and strategy~\cite{tran2025}.

The key enabler of multi-agent capability is inter-agent communication, which allows agents to exchange intermediate results and coordinate plans~\cite{yan2025}. Empirical evidence for multi-agent advantages is documented across several domains. LongAgent~\cite{zhao2024longagent} scales language models to 128K effective context through multi-agent collaboration, precisely because individual agents lose information over long inputs. Chain-of-Agents~\cite{wang2024coa} assigns each worker agent a short text segment and has a manager agent synthesise their outputs, outperforming RAG and full-context baselines by up to 10\% on QA and summarisation tasks. \cite{du2023} show that multi-agent debate improves factuality and reasoning across six tasks~\cite{du2023}, and AgentVerse~\cite{chen2024agentverse} consistently produced multi-agent groups that outperformed single agents, with the most significant gains on tasks requiring diverse expertise.

The advantage is not unconditional, however. \cite{wang2024} show that a MAS with suboptimally designed collaboration channels can be overtaken by a single agent with strong prompting~\cite{wang2024}---an important caveat for the comparisons undertaken in this paper.

\paragraph{Communication patterns.}
\cite{guo2024} identify four structural patterns governing inter-agent context flow: layered (hierarchical, adjacent-layer interaction), decentralised (peer-to-peer), centralised (a coordinating agent), and shared message pool (publish-subscribe)~\cite{guo2024}. \cite{yan2025} provide a more detailed taxonomy that views communication from multiple angles---architecture, protocol, and strategy---and defines five primary architectures: flat, hierarchical, team, society, and hybrid~\cite{yan2025}. Broadly, flat corresponds to the decentralised pattern of \cite{guo2024}, while hierarchical encompasses both centralised and layered variants.

On the strategy dimension, \cite{yan2025} distinguish One-by-One (sequential turn-taking, each agent integrating all prior messages before responding), Simultaneous-Talk (parallel communication without turn-taking, faster but requiring arbitration for conflicting proposals), and Simultaneous-Talk-with-Summariser (a dedicated consolidation step after parallel turns)~\cite{yan2025}. Chain-of-Agents~\cite{wang2024coa} is an instance of the One-by-One strategy applied to long-document tasks.

A recurring finding across the literature is the cascading error effect: in sequential architectures, one agent's hallucination or error propagates forward and is accepted by subsequent agents~\cite{guo2024,tran2025}. Unauthorised modification of shared memory can similarly cause systemic failures~\cite{han2024}.

\subsection{Agentic Frameworks}

The proliferation of LLM-MAS frameworks can be understood as competing answers to a common design question: what information enters each agent's context, and when? The four most widely used general-purpose frameworks are LangGraph, Google ADK, CrewAI, and AutoGen.

LangGraph treats agent workflows as explicit directed graphs: agents are nodes, data flows along typed edges, and a shared state object sits at the centre. Agents communicate by reading and writing to this centralised state rather than exchanging raw messages; each agent processes the current state and returns an updated version. A swarm mode supports dynamic handoffs, where the active-agent field is updated and the necessary context is forwarded to the next agent.

Google ADK is organised as a hierarchical tree of specialised agents orchestrated by typed workflow primitives. Context is shared through three mechanisms: a shared session state (a persistent whiteboard readable by all agents), LLM-driven delegation (a parent coordinator dynamically routes to sub-agents), and explicit tool invocation (one agent calls another as a tool and receives only its result).

AutoGen's original philosophy is conversational: agents interact through message exchanges, each embodying a specific persona and skill set. In group chat mode a centralised Group Chat Manager orchestrates turn order; all agents see the full message thread, which is natural but token-heavy. A more recently released Agent Framework adds graph-based workflows and native thread management with persistent memory for more deterministic coordination.

CrewAI follows a role-based team simulation model. Tasks pass their output directly as context to the next task or agent in the pipeline. When conversation history grows too large, CrewAI can automatically summarise it, so downstream agents receive structured prior output or a summary rather than raw full history.

A practical difficulty for researchers and developers is that the same underlying mechanism appears under different names across frameworks and papers. Shared message pool, broadcasting, and blackboard all describe the same pattern of a persistent shared state that multiple agents read from and write to; this corresponds to ADK's session state and CrewAI's task output. Chat chain, message exchange, and One-by-One strategy all refer to sequential turn-based context passing. Similarly, static architectures~\cite{tran2025}---those using predefined rules to establish collaboration channels---correspond to what ADK calls workflow agents and LangGraph calls compiled graphs, while dynamic architectures, which adapt to evolving task requirements, correspond to what frameworks label agentic execution. The same term can also carry different meanings: \emph{centralised} in \cite{chen2023robots} refers to a single LLM receiving all observations and emitting all commands, whereas in \cite{han2024} it refers to a central agent that coordinates peer communication without executing their tasks. This paper adopts the vocabulary of \cite{yan2025} as a shared reference where possible.

\subsection{Handoff versus Sub-Agents-as-Tools}
\label{sec:handoff_vs_tool}

The two delegation patterns examined in this paper are present across frameworks and research papers under a variety of names; Table~\ref{tab:pattern_equivalences} maps the corresponding terms used for each pattern across the literature.

\begin{table}[ht]
\centering
\caption{Parallel terminology for the two delegation patterns across the literature.}
\label{tab:pattern_equivalences}
\begin{tabular}{p{0.10\textwidth}p{0.40\textwidth}p{0.40\textwidth}}
\toprule
Reference & Sub-agent as Tool & Handoff \\
\midrule
\cite{han2024} & Nested structure & Equi-level structure \\
\cite{yan2025} & Hierarchical architecture; message passing & Flat architecture; one-to-one \\
\cite{guo2024} & Centralised communication & Decentralised communication \\
\bottomrule
\end{tabular}
\end{table}

In the sub-agent-as-tool pattern, an orchestrating agent invokes a specialised agent as a callable function. The sub-agent executes its designated task and returns a result; it does not receive prior conversation context and does not interact with the user directly. Overall conversation management remains with the primary agent. This corresponds to the nested structure of \cite{han2024}, the hierarchical communication architecture and message-passing paradigm of \cite{yan2025}, and the centralised communication pattern of \cite{guo2024}.

In the handoff pattern, one agent transfers control of an ongoing task to another peer agent, passing along task state, conversation history, and relevant metadata. The receiving agent takes over the interaction directly. This corresponds to the equi-level structure~\cite{han2024}, flat architecture and one-to-one paradigm~\cite{yan2025}, and decentralised communication pattern~\cite{guo2024}.

The surveyed literature surfaces several relevant trade-offs, though none of the prior work directly compares these two patterns in a controlled experiment. \cite{yan2025} note that hierarchical systems may encounter bottlenecks when higher-level agents become overloaded or communication delays between layers accumulate, while flat architectures excel in agile, spontaneous interaction but face scalability challenges as agent count grows~\cite{yan2025}. Token usage is also underexplored: while it is established that multi-agent systems generally require more tokens than single-agent counterparts~\cite{tran2025}, it is not clear whether sub-agent-as-tool or handoff incurs a greater cost, since both involve multiple agents.

\subsection{Benchmarks}
\label{sec:benchmarks}

General-purpose agent evaluation is addressed by AgentBench~\cite{liu2023agentbench}, which evaluates LLMs as agents across diverse environments (OS, database, web, game), and MLAgentBench~\cite{huang2024mlagentbench}, which tests autonomous ML experimentation---both in single-agent settings. Tool use has attracted dedicated benchmarks: LiveMCPBench~\cite{livemcpbench} evaluates a single-agent system acting as a router across 70+ MCP servers, while MCP-Bench~\cite{mcpbench} assesses individual LLMs on multi-hop tasks that require chaining several tools.

Evaluation of multi-agent systems is less common and inherently more complex, as it must account not only for task completion but also for coordination efficiency, communication bandwidth, and latency. MultiAgentBench~\cite{zhu2025multiagentbench} assesses coordination through milestone-based KPIs across 13 tasks spanning image classification to open research challenges. Four coordination topologies---star, chain, tree, and graph---are evaluated alongside strategies such as group discussion and cognitive planning. Graph topology performed best on knowledge-intensive tasks, and cognitive planning improved milestone achievement by 3\% over baselines, suggesting that dense peer-to-peer communication outperforms hierarchical or chain structures for research-style collaboration.

REALM-Bench~\cite{geng2025realmbench} focuses on 14 planning and scheduling problems---logistics scenarios and job-shop scheduling---scaled along three dimensions: the number of parallel planning threads, the complexity of inter-agent dependencies, and the frequency of real-time disruptions. Evaluated across LangGraph and a custom hierarchical framework using GPT-4o, Claude 3.7, and DeepSeek-R1, the benchmark finds that multi-agent systems outperform standalone LLMs on optimisation tasks, but that agents fail to leverage early disruption signals and largely lack robust conflict-resolution mechanisms. The most directly relevant empirical comparison in the surveyed literature is \cite{chen2023robots}, which explicitly compares centralised, decentralised, and two hybrid communication frameworks for multi-robot coordination, finding domain-dependent trade-offs rather than a universally superior option.

No existing benchmark specifically compares sub-agent-as-tool and handoff delegation under equivalent task conditions. Closing this gap is the principal motivation for the present work.