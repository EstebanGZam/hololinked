# hololinked

### Description

For beginners - `hololinked` is an instrumentation control and data acquisition over network purely based on python.
<br/> 
For those familiar with RPC & web development - `hololinked` is a ZMQ-based RPC toolkit with customizable HTTP end-points. 
The main goal is, as stated before, to develop a pythonic (& pure python) modern package for instrument control & data acquisition 
through network (SCADA), along with native HTTP support for communication with browser clients for browser based UIs.  
This package can also be used for general RPC/controlling general python object instances on network for other applications
like computational algorithms, running scripts etc.. 
<br />

##### NOTE - The package is rather incomplete and uploaded only for API showcase and active development. Even RPC logic is not complete. <br/>

- documentation and tutorial webpage - https://hololinked.readthedocs.io/en/latest/
- example repository - https://github.com/VigneshVSV/hololinked-examples
- helper GUI - https://github.com/VigneshVSV/hololinked-portal
- custom GUI examples - (will be filled)

### Already Existing Features

Support is already present for the following:

- decorate HTTP verbs directly on object's methods
- declare parameters (based on [`param`](https://param.holoviz.org/getting_started.html)) for validated object attributes on the network
- create named events (based on ZMQ) for asychronous communication with clients. These events are also tunneled as HTTP server-sent events
- control method execution and parameter(or attribute) write with finite state machine
- use serializer of your choice (except for HTTP) - Serpent, JSON, pickle etc. & extend serialization to suit your requirement (HTTP Server will support only JSON serializer)
- asyncio compatible - async RPC Server event-loop and async HTTP Server 
- have flexibility in process architecture - run HTTP Server & python object in separate processes or in the same process, combine multiple objects in same server etc. 
- choose from one of multiple ZMQ Protocols - TCP for network access, and IPC for multi-process same-PC applications at improved speed. 
Optionally INPROC for multi-threaded applications. 

Again, please check examples for how-to & explanations of the above. 

### To Install

clone the repository and install in develop mode `pip install -e .` for convenience. The conda env hololinked.yml can also help. 

### In Development

- Object Proxy - only a very rudimentary implementation exists now.
- HTTP 2.0 
- Database support for storing and loading parameters (based on SQLAlchemy) when object dies and restarts


[![Documentation Status](https://readthedocs.org/projects/hololinked/badge/?version=latest)](https://hololinked.readthedocs.io/en/latest/?badge=latest)
 



