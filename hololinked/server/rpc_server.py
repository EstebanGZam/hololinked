import zmq
import zmq.asyncio
import sys 
import os
import warnings
import asyncio
import importlib
import typing 
import threading
import logging
import tracemalloc
from collections import deque
from uuid import uuid4


from .constants import JSON, ZMQ_TRANSPORT_LAYERS, ServerTypes
from .utils import format_exception_as_json, get_current_async_loop, get_default_logger
from .config import global_config
from .exceptions import *
from .thing import Thing, ThingMeta
from .property import Property
from .properties import TypedList, Boolean, TypedDict
from .actions import Action, action as remote_method
from .logger import ListHandler

from .protocols.zmq.brokers import (CM_INDEX_ADDRESS, CM_INDEX_CLIENT_TYPE, CM_INDEX_MESSAGE_TYPE, CM_INDEX_MESSAGE_ID, 
                                CM_INDEX_SERVER_EXEC_CONTEXT, CM_INDEX_THING_ID)
from .protocols.zmq.brokers import SM_INDEX_ADDRESS
from .protocols.zmq.brokers import EXIT, HANDSHAKE, INVALID_MESSAGE, TIMEOUT
from .protocols.zmq.brokers import HTTP_SERVER, PROXY, TUNNELER 
from .protocols.zmq.brokers import EMPTY_DICT
from .protocols.zmq.brokers import (AsyncZMQClient, AsyncZMQServer, BaseZMQServer, 
                                  EventPublisher, SyncZMQClient)
from .protocols.zmq.brokers import RequestMessage


if global_config.TRACE_MALLOC:
    tracemalloc.start()

def set_event_loop_policy():
    if sys.platform.lower().startswith('win'):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    if global_config.USE_UVLOOP:
        if sys.platform.lower() in ['linux', 'darwin', 'linux2']:
            import uvloop
            asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
        else:
            warnings.warn("uvloop not supported for windows, using default windows selector loop.", RuntimeWarning)

set_event_loop_policy()



RemoteObject = Thing # reading convenience

class RPCServer(BaseZMQServer):
    """
    The EventLoop class implements a infinite loop where zmq ROUTER sockets listen for messages. Each consumer of the 
    event loop (an instance of Thing) listen on their own ROUTER socket and execute methods or allow read and write
    of attributes upon receiving instructions. Socket listening is implemented in an async (asyncio) fashion. 
  
    Top level ZMQ RPC server used by ``Thing`` and ``Eventloop``. 

    Parameters
    ----------
    instance_name: str
        ``instance_name`` of the server
    server_type: str
        server type metadata
    context: Optional, zmq.asyncio.Context
        ZeroMQ async Context object to use. All sockets except those created by event publisher share this context. 
        Automatically created when None is supplied.
    **kwargs:
        tcp_socket_address: str
            address of the TCP socket, if not given, a random port is chosen
    """
    
    expose = Boolean(default=True, remote=False,
                     doc="""set to False to use the object locally to avoid alloting network resources 
                        of your computer for this object""")

    things = TypedDict(key_type=(bytes,), item_type=(Thing,), bounds=(0,100), allow_None=True, default=None,
                        doc="list of Things which are being executed", remote=False) # type: typing.Dict[bytes, Thing]
    
    threaded = Boolean(default=False, remote=False, 
                        doc="set True to run each thing in its own thread")
  

    def __init__(self, *, instance_name : str, things : typing.List[Thing],
               context : typing.Union[zmq.asyncio.Context, None] = None, 
                **kwargs
            ) -> None:
        """
        Parameters
        ----------
        instance_name: str
            instance name of the event loop
        things: List[Thing]
            things to be run/served
        log_level: int
            log level of the event loop logger
        """
        BaseZMQServer.__init__(self, instance_name=instance_name, server_type=ServerTypes.RPC.value, 
                                **kwargs)
        self.uninstantiated_things = dict()
        self.things = dict() # typing.Dict[bytes, Thing]
        for thing in things:
            self.things[bytes(thing.id, encoding='utf-8')] = thing

        kwargs["http_serializer"] = self.http_serializer
        kwargs["zmq_serializer"] = self.zmq_serializer
        # RemoteObject.__init__(self, instance_name=instance_name, logger=self.logger, things=things,
        #                         logger_remote_access=False, **kwargs)
        # if self.expose:
        #     self.things.append(self)      

        if self.logger is None:
            self.logger =  get_default_logger('{}|{}|{}|{}'.format(self.__class__.__name__, 
                                                'RPC', 'MIXED', self.instance_name), kwargs.get('log_level', logging.INFO))
        # contexts and poller
        self._terminate_context = context is None
        self.context = context or zmq.asyncio.Context()

       
        self.inproc_server = AsyncZMQServer(
                                instance_name=self.instance_name, 
                                server_type=ServerTypes.RPC, 
                                context=self.context, 
                                protocol=ZMQ_TRANSPORT_LAYERS.INPROC, 
                                **kwargs
                            )        
        self.event_publisher = EventPublisher(
                                instance_name=self.instance_name + '-event-pub',
                                protocol=ZMQ_TRANSPORT_LAYERS.INPROC,
                                logger=self.logger,
                                **kwargs
                            )        
        # message serializing broker
        for instance in self.things.values():
            instance._inproc_client = AsyncZMQClient(
                                        server_instance_name=instance.id,
                                        identity=f'{self.instance_name}/tunneler',
                                        client_type=TUNNELER, 
                                        context=self.context, 
                                        protocol=ZMQ_TRANSPORT_LAYERS.INPROC, 
                                        logger=self.logger,
                                        handshake=False # handshake manually done later when event loop is run
                                    )
            instance._inproc_server = AsyncZMQServer(
                                        instance_name=instance.id,
                                        server_type=ServerTypes.THING,
                                        context=self.context,
                                        protocol=ZMQ_TRANSPORT_LAYERS.INPROC,
                                        logger=self.logger,
                                        **kwargs
                                    ) 
            instance._zmq_messages = deque()
            instance._zmq_messages_event = asyncio.Event()
            instance.rpc_server = self
            instance.event_publisher = self.event_publisher 
            instance._prepare_resources()
     
        
    def __post_init__(self):
        super().__post_init__()
        self.logger.info("Server with name '{}' can be started using run().".format(self.instance_name))   


    async def recv_requests(self, server : AsyncZMQServer):
        """
        Continuously keeps receiving messages from different clients and appends them to a deque to be processed 
        sequentially. Also handles messages that dont need to be queued like HANDSHAKE, EXIT, invokation timeouts etc.
        """
        eventloop = asyncio.get_event_loop()
        socket = server.socket
        while True:
            try:
                raw_message = await socket.recv_multipart()
                message = RequestMessage(raw_message)
                client_addr, client_type, message_id, message_type, server_execution_context = message.header
                # handle message types first
                if message_type == HANDSHAKE:
                    handshake_task = asyncio.create_task(self._handshake(message, socket))
                    self.eventloop.call_soon(lambda : handshake_task)
                    continue 
                if message_type == EXIT:
                    break
                
                self.logger.debug(f"received message from client '{client_addr}' with message id '{message_id}', queuing.")
                # handle invokation timeout
                invokation_timeout = server_execution_context.get("invokation_timeout", None)

                # schedule to tunnel it to thing
                ready_to_process_event = None
                timeout_task = None
                if invokation_timeout is not None:
                    ready_to_process_event = asyncio.Event()
                    timeout_task = asyncio.create_task(self.process_timeouts(message, 
                                                ready_to_process_event, invokation_timeout, socket, 'invokation'))
                    eventloop.call_soon(lambda : timeout_task)
            except Exception as ex:
                # handle invalid message
                self.logger.error(f"exception occurred for message id '{message[CM_INDEX_MESSAGE_ID]}' - {str(ex)}")
                invalid_message_task = asyncio.create_task(
                                                self._handle_invalid_message(
                                                    originating_socket=socket,
                                                    original_client_message=message,
                                                    exception=ex
                                                )
                                            )
                eventloop.call_soon(lambda: invalid_message_task)
            else:
                instance = self.things[message.thing_id]
                # append to messages list - message, execution context, event, timeout task, origin socket
                instance._zmq_messages.append((message, ready_to_process_event, timeout_task, socket))
                instance._zmq_messages_event.set() # this was previously outside the else block - reason unknown now
        self.logger.info(f"stopped polling for server '{server.identity}' {server.socket_address[0:3].upper() if server.socket_address[0:3] in ['ipc', 'tcp'] else 'INPROC'}")
           
 
    async def tunnel_message_to_things(self, instance : Thing) -> None:
        """
        message tunneler between external sockets and interal inproc client
        """
        while not self.stop_poll:
            if len(instance._zmq_messages) == 0:
                await instance._zmq_messages_event.wait()
                instance._zmq_messages_event.clear()
                # this means in next loop it wont be in this block as a message arrived  
                continue
            # retrieve from messages list - message, execution context, event, timeout task, origin socket
            message, ready_to_process_event, timeout_task, origin_socket = instance._zmq_messages.popleft()
        
            invokation_timed_out = True 
            if ready_to_process_event is not None: 
                ready_to_process_event.set() # releases timeout task 
                invokation_timed_out = await timeout_task
            if ready_to_process_event is not None and invokation_timed_out:
                # drop call to thing
                continue 
            # handle execution through thing
            original_address = message[CM_INDEX_ADDRESS]
            message[CM_INDEX_ADDRESS] = message[CM_INDEX_THING_ID] 
            # replace RPC server address with thing ID to route correctly
            await instance._inproc_client.socket.send_multipart(message)

            client_addr, client_type, message_id, message_type, server_execution_context = message.header
            instance.logger.debug(f"client {client_addr} of client type {client_type} issued operation " +
                                f"{operation} with message id {msg_id}. starting execution.")
            # schedule an execution timeout
            execution_timeout = server_execution_context.get("execution_timeout", None)
            execution_completed_event = None 
            execution_timeout_task = None
            execution_timed_out = True
            if execution_timeout is not None:
                execution_completed_event = asyncio.Event()
                execution_timeout_task = asyncio.create_task(self.process_timeouts(message, 
                                        execution_completed_event, execution_timeout, origin_socket, 'execution'))
                self.eventloop.call_soon(lambda : execution_timeout_task)
            # always wait for reply from thing, since this loop is asyncio task (& in its own thread in RPC server), 
            # timeouts always reach client without truly blocking by the GIL. If reply does not arrive, all other requests
            # get invokation timeout.
            reply = await instance._inproc_client.socket.recv_multipart()
            reply[SM_INDEX_ADDRESS] = original_address
            # check if execution completed within time
            if execution_completed_event is not None:
                execution_completed_event.set() # releases timeout task
                execution_timed_out = await execution_timeout_task
            if execution_timeout_task is not None and execution_timed_out:
                # drop reply to client as timeout was already sent
                continue
            if server_execution_context.get("oneway", False):
                # drop reply if oneway
                continue 
            await origin_socket.send_multipart(reply)    
        self.logger.info("stopped tunneling messages to things")


    async def process_timeouts(self, original_client_message : typing.List, ready_to_process_event : asyncio.Event,
                               timeout : typing.Optional[float], origin_socket : zmq.Socket, timeout_typ : str) -> bool:
        """
        replies timeout to client if timeout occured and prevents the message from being executed. 
        """
        try:
            await asyncio.wait_for(ready_to_process_event.wait(), timeout)
            return False 
        except TimeoutError:    
            await origin_socket.send_multipart(self.craft_response_from_arguments(
                address=original_client_message[CM_INDEX_ADDRESS], client_type=original_client_message[CM_INDEX_CLIENT_TYPE], 
                message_type=TIMEOUT, message_id=original_client_message[CM_INDEX_MESSAGE_ID], data=timeout_typ
            ))
            return True


    async def _handle_invalid_message(self, originating_socket : zmq.Socket, original_client_message: typing.List[bytes], 
                                exception: Exception) -> None:
        await originating_socket.send_multipart(self.craft_response_from_arguments(
                            original_client_message[CM_INDEX_ADDRESS], original_client_message[CM_INDEX_CLIENT_TYPE], 
                            INVALID_MESSAGE, original_client_message[CM_INDEX_MESSAGE_ID], exception))
        self.logger.info(f"sent exception message to client '{original_client_message[CM_INDEX_ADDRESS]}'." +
                            f" exception - {str(exception)}") 	
    

    async def _handshake(self, original_client_message: typing.List[bytes],
                                    originating_socket : zmq.Socket) -> None:
        await originating_socket.send_multipart(self.craft_response_from_arguments(
                original_client_message[CM_INDEX_ADDRESS], 
                original_client_message[CM_INDEX_CLIENT_TYPE], HANDSHAKE, original_client_message[CM_INDEX_MESSAGE_ID],
                EMPTY_DICT))
        self.logger.info("sent handshake to client '{}'".format(original_client_message[CM_INDEX_ADDRESS]))


    async def poll(self):
        """
        poll for messages and append them to messages list to pass them to ``Eventloop``/``Thing``'s inproc 
        server using an inner inproc client. Registers the messages for timeout calculation.
        """
        self.stop_poll = False
        self.eventloop = asyncio.get_event_loop()
        self.inner_inproc_client.handshake()
        await self.inner_inproc_client.handshake_complete()
        if self.inproc_server:
            self.eventloop.call_soon(lambda : asyncio.create_task(self.recv_request(self.inproc_server)))
        if self.ipc_server:
            self.eventloop.call_soon(lambda : asyncio.create_task(self.recv_request(self.ipc_server)))
        if self.tcp_server:
            self.eventloop.call_soon(lambda : asyncio.create_task(self.recv_request(self.tcp_server)))
       

    def stop_polling(self):
        """
        stop polling method ``poll()``
        """
        self.stop_poll = True
        instance._messages_event.set()
        if self.inproc_server is not None:
            def kill_inproc_server(instance_name, context, logger):
                # this function does not work when written fully async - reason is unknown
                try: 
                    event_loop = asyncio.get_event_loop()
                except RuntimeError:
                    event_loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(event_loop)
                temp_inproc_client = AsyncZMQClient(server_instance_name=instance_name,
                                            identity=f'{self.instance_name}-inproc-killer',
                                            context=context, client_type=PROXY, protocol=ZMQ_TRANSPORT_LAYERS.INPROC, 
                                            logger=logger) 
                event_loop.run_until_complete(temp_inproc_client.handshake_complete())
                event_loop.run_until_complete(temp_inproc_client.socket.send_multipart(temp_inproc_client.craft_empty_message_with_type(EXIT)))
                temp_inproc_client.exit()
            threading.Thread(target=kill_inproc_server, args=(self.instance_name, self.context, self.logger), daemon=True).start()
        if self.ipc_server is not None:
            temp_client = SyncZMQClient(server_instance_name=self.instance_name, identity=f'{self.instance_name}-ipc-killer',
                                    client_type=PROXY, protocol=ZMQ_TRANSPORT_LAYERS.IPC, logger=self.logger) 
            temp_client.socket.send_multipart(temp_client.craft_empty_message_with_type(EXIT))
            temp_client.exit()
        if self.tcp_server is not None:
            socket_address = self.tcp_server.socket_address
            if '/*:' in self.tcp_server.socket_address:
                socket_address = self.tcp_server.socket_address.replace('*', 'localhost')
            # print("TCP socket address", self.tcp_server.socket_address)
            temp_client = SyncZMQClient(server_instance_name=self.instance_name, identity=f'{self.instance_name}-tcp-killer',
                                    client_type=PROXY, protocol=ZMQ_TRANSPORT_LAYERS.TCP, logger=self.logger,
                                    socket_address=socket_address)
            temp_client.socket.send_multipart(temp_client.craft_empty_message_with_type(EXIT))
            temp_client.exit()   


    async def run_single_target(self, instance : Thing) -> None: 
        while True:
            messages = await instance._inproc_server.async_recv_requests()
            for message in messages:
                # client, mty_byte_1, client_type, msg_type, msg_id, server_execution_context, mty_byte_2, 
                thing_id, objekt, operation, arguments, execution_context = message.body
                fetch_execution_logs = execution_context.pop("fetch_execution_logs", False)
                if fetch_execution_logs:
                    list_handler = ListHandler([])
                    list_handler.setLevel(logging.DEBUG)
                    list_handler.setFormatter(instance.logger.handlers[0].formatter)
                    instance.logger.addHandler(list_handler)
                try:
                    return_value = await self.execute_once(instance, objekt, operation, arguments) 
                    if fetch_execution_logs:
                        return_value = {
                            "return_value" : return_value,
                            "execution_logs" : list_handler.log_list
                        }
                    await instance.message_broker.async_send_response(message, return_value)
                    # Also catches exception in sending messages like serialization error
                except (BreakInnerLoop, BreakAllLoops):
                    instance.logger.info("Thing {} with instance name {} exiting event loop.".format(
                                                            instance.__class__.__name__, instance.instance_name))
                    return_value = None
                    if fetch_execution_logs:
                        return_value = { 
                            "return_value" : None,
                            "execution_logs" : list_handler.log_list
                        }
                    await instance.message_broker.async_send_response(message, return_value)
                    return 
                except Exception as ex:
                    instance.logger.error("Thing {} with instance name {} produced error : {}.".format(
                                                            instance.__class__.__name__, instance.instance_name, ex))
                    return_value = dict(exception=format_exception_as_json(ex))
                    if fetch_execution_logs:
                        return_value["execution_logs"] = list_handler.log_list
                    await instance.message_broker.async_send_response_with_message_type(message, 
                                                                    b'EXCEPTION', return_value)
                finally:
                    if fetch_execution_logs:
                        instance.logger.removeHandler(list_handler)

   
    async def execute_once(cls, instance : Thing, objekt : typing.Optional[str], operation : str,
                               arguments : typing.Dict[str, typing.Any]) -> typing.Any:
        if operation == b'readProperty':
            prop = instance.properties[objekt] # type: Property
            return getattr(instance, prop.name) 
        elif operation == b'writeProperty':
            prop = instance.properties[objekt] # type: Property
            return prop.external_set(instance, arguments) # external set has state machine logic inside
        elif operation == b'deleteProperty':
            prop = instance.properties[objekt] # type: Property
            del prop # raises NotImplementedError when deletion is not implemented which is mostly the case
        elif operation == b'invokeAction':
            action = instance.actions[objekt] # type: Action
            args = arguments.pop('__args__', tuple())
            # arguments then become kwargs
            if action.execution_info.iscoroutine:
                return await action.external_call(*args, **arguments) 
            else:
                return action.external_call(*args, **arguments) 
        elif operation == b'readMultipleProperties' or operation == b'readAllProperties':
            if objekt is None:
                return instance._get_properties()
            return instance._get_properties(names=objekt)
        elif operation == b'writeMultipleProperties' or operation == b'writeAllProperties':
            return instance._set_properties(arguments)
        # elif operation == b'dataResponse': # no operation defined yet for dataResponse to events in Thing Description
        #     from .events import CriticalEvent
        #     event = instance.events[objekt] # type: CriticalEvent, this name "CriticalEvent" needs to change, may be just plain Event
        #     return event._set_acknowledgement(**arguments)
        raise NotImplementedError("Unimplemented execution path for Thing {} for operation {}".format(
                                                                        instance.instance_name, operation))


    def run_external_message_listener(self):
        """
        Runs ZMQ's sockets which are visible to clients.
        This method is automatically called by ``run()`` method. 
        Please dont call this method when the async loop is already running. 
        """
        self.request_listener_loop = get_current_async_loop()
        futures = []
        futures.append(self.poll())
        futures.append(self.tunnel_message_to_things())
        self.logger.info("starting external message listener thread")
        self.request_listener_loop.run_until_complete(asyncio.gather(*futures))
        pending_tasks = asyncio.all_tasks(self.request_listener_loop)
        self.request_listener_loop.run_until_complete(asyncio.gather(*pending_tasks))
        self.logger.info("exiting external listener event loop {}".format(self.instance_name))
        self.request_listener_loop.close()
    

    def run_things_executor(self, things : typing.List[Thing]):
        """
        Run ZMQ sockets which provide queued instructions to ``Thing``.
        This method is automatically called by ``run()`` method. 
        Please dont call this method when the async loop is already running. 
        """
        thing_executor_loop = get_current_async_loop()
        self.thing_executor_loop = thing_executor_loop # atomic assignment for thread safety
        self.logger.info(f"starting thing executor loop in thread {threading.get_ident()} for {[obj.instance_name for obj in things]}")
        thing_executor_loop.run_until_complete(
            asyncio.gather(*[self.run_single_target(instance) for instance in things])
        )
        self.logger.info(f"exiting event loop in thread {threading.get_ident()}")
        thing_executor_loop.close()


    def exit(self):
        self.stop_poll = True
        for socket in list(self.poller._map.keys()): # iterating over keys will cause dictionary size change during iteration
            try:
                self.poller.unregister(socket)
            except Exception as ex:
                self.logger.warning(f"could not unregister socket from polling - {str(ex)}") # does not give info about socket
        try:
            if self.inproc_server is not None:
                self.inproc_server.exit()
            if self.ipc_server is not None:
                self.ipc_server.exit()
            if self.tcp_server is not None:
                self.tcp_server.exit()
            if self.inner_inproc_client is not None:
                self.inner_inproc_client.exit()
            if self.event_publisher is not None:
                self.event_publisher.exit()
        except:
            pass 
        # if self._terminate_context:
        #     self.context.term()
        self.logger.info("terminated context of socket '{}' of type '{}'".format(self.instance_name, self.__class__))


    async def handshake_complete(self):
        """
        handles inproc client's handshake with ``Thing``'s inproc server
        """
        await self.inner_inproc_client.handshake_complete()


    # example of overloading
    # @remote_method()
    # def exit(self):
    #     """
    #     Stops the event loop and all its things. Generally, this leads
    #     to exiting the program unless some code follows the ``run()`` method.  
    #     """
    #     for thing in self.things:
    #         thing.exit()
    #     raise BreakAllLoops
    

    uninstantiated_things = TypedDict(default=None, allow_None=True, key_type=str,
                                            item_type=str)
    
    
    @classmethod
    def _import_thing(cls, file_name : str, object_name : str):
        """
        import a thing specified by ``object_name`` from its 
        script or module. 

        Parameters
        ----------
        file_name : str
            file or module path 
        object_name : str
            name of ``Thing`` class to be imported
        """
        module_name = file_name.split(os.sep)[-1]
        spec = importlib.util.spec_from_file_location(module_name, file_name)
        if spec is not None:
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        else:     
            module = importlib.import_module(module_name, file_name.split(os.sep)[0])
        consumer = getattr(module, object_name) 
        if issubclass(consumer, Thing):
            return consumer 
        else:
            raise ValueError(f"object name {object_name} in {file_name} not a subclass of Thing.", 
                            f" Only subclasses are accepted (not even instances). Given object : {consumer}")
        

    @remote_method()
    def import_thing(self, file_name : str, object_name : str):
        """
        import thing from the specified path and return the default 
        properties to be supplied to instantiate the object. 
        """
        consumer = self._import_thing(file_name, object_name) # type: ThingMeta
        id = uuid4()
        self.uninstantiated_things[id] = consumer
        return id
           

    @remote_method() # remember to pass schema with mandatory instance name
    def instantiate(self, id : str, kwargs : typing.Dict = {}):      
        """
        Instantiate the thing that was imported with given arguments 
        and add to the event loop
        """
        consumer = self.uninstantiated_things[id]
        instance = consumer(**kwargs, eventloop_name=self.instance_name) # type: Thing
        self.things.append(instance)
        rpc_server = instance.rpc_server
        self.request_listener_loop.call_soon(asyncio.create_task(lambda : rpc_server.poll()))
        self.request_listener_loop.call_soon(asyncio.create_task(lambda : rpc_server.tunnel_message_to_things()))
        if not self.threaded:
            self.thing_executor_loop.call_soon(asyncio.create_task(lambda : self.run_single_target(instance)))
        else: 
            _thing_executor = threading.Thread(target=self.run_things_executor, args=([instance],))
            _thing_executor.start()


    def run(self):
        """
        start the eventloop
        """
        if not self.threaded:
            _thing_executor = threading.Thread(target=self.run_things_executor, args=(self.things,))
            _thing_executor.start()
        else: 
            for thing in self.things:
                _thing_executor = threading.Thread(target=self.run_things_executor, args=([thing],))
                _thing_executor.start()
        self.run_external_message_listener()
        if not self.threaded:
            _thing_executor.join()



__all__ = [
    RPCServer.__name__
]




class ZMQServer:
    

    def __init__(self, *, instance_name : str, 
                things : typing.Union[Thing, typing.List[typing.Union[Thing]]], # type: ignore - requires covariant types
                protocols : typing.Union[ZMQ_TRANSPORT_LAYERS, str, typing.List[ZMQ_TRANSPORT_LAYERS]] = ZMQ_TRANSPORT_LAYERS.IPC, 
                poll_timeout = 25, context : typing.Union[zmq.asyncio.Context, None] = None, 
                **kwargs
            ) -> None:
        self.inproc_server = self.ipc_server = self.tcp_server = self.event_publisher = None
        
        if isinstance(protocols, str): 
            protocols = [protocols]
        elif not isinstance(protocols, list): 
            raise TypeError(f"unsupported protocols type : {type(protocols)}")
        tcp_socket_address = kwargs.pop('tcp_socket_address', None)
        event_publisher_protocol = None 
        
        # initialise every externally visible protocol          
        if ZMQ_TRANSPORT_LAYERS.TCP in protocols or "TCP" in protocols:
            self.tcp_server = AsyncZMQServer(instance_name=self.instance_name, server_type=ServerTypes.RPC, 
                                    context=self.context, protocol=ZMQ_TRANSPORT_LAYERS.TCP, poll_timeout=poll_timeout, 
                                    socket_address=tcp_socket_address, **kwargs)
            self.poller.register(self.tcp_server.socket, zmq.POLLIN)
            event_publisher_protocol = ZMQ_TRANSPORT_LAYERS.TCP
        if ZMQ_TRANSPORT_LAYERS.IPC in protocols or "IPC" in protocols: 
            self.ipc_server = AsyncZMQServer(instance_name=self.instance_name, server_type=ServerTypes.RPC, 
                                    context=self.context, protocol=ZMQ_TRANSPORT_LAYERS.IPC, poll_timeout=poll_timeout, **kwargs)
            self.poller.register(self.ipc_server.socket, zmq.POLLIN)
            event_publisher_protocol = ZMQ_TRANSPORT_LAYERS.IPC if not event_publisher_protocol else event_publisher_protocol           
            event_publisher_protocol = "IPC" if not event_publisher_protocol else event_publisher_protocol    

        self.poller = zmq.asyncio.Poller()
        self.poll_timeout = poll_timeout

    
    @property
    def poll_timeout(self) -> int:
        """
        socket polling timeout in milliseconds greater than 0. 
        """
        return self._poll_timeout

    @poll_timeout.setter
    def poll_timeout(self, value) -> None:
        if not isinstance(value, int) or value < 0:
            raise ValueError(("polling period must be an integer greater than 0, not {}.",
                              "Value is considered in milliseconds.".format(value)))
        self._poll_timeout = value 